"""Unit tests for the eval runner: stream parsing, fixture rendering, arm checks, graders.

The stream fixture is a real `claude -p --output-format stream-json --verbose` capture
(haiku, one denied `git commit`, one `date`), with machine paths stripped from `init`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import run

FIXTURES = Path(__file__).parent / "fixtures"
CT = ZoneInfo("America/Chicago")
LINE = "Now 16:24 CDT · Launch in 7h35m (23:59 CDT)"


def record(t_start: datetime, t_end: datetime) -> run.RunRecord:
    stream = run.load_stream(FIXTURES / "stream-denied-commit.jsonl")
    return run.RunRecord(stream=stream, t_start=t_start, t_end=t_end, tz="America/Chicago", fixture_dir=Path("/nonexistent"))


def at(hh: int, mm: int) -> datetime:
    return datetime(2026, 9, 16, hh, mm, tzinfo=CT)


class StreamTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = run.load_stream(FIXTURES / "stream-denied-commit.jsonl")

    def test_tool_uses_in_order(self) -> None:
        self.assertEqual([t["name"] for t in self.stream.tool_uses], ["Bash", "Bash"])
        self.assertEqual(self.stream.tool_uses[0]["input"]["command"], "git commit -m probe")
        self.assertEqual(self.stream.tool_uses[1]["input"]["command"], "date")

    def test_denied_call_is_recorded(self) -> None:
        self.assertEqual(self.stream.denied_ids, {self.stream.tool_uses[0]["id"]})

    def test_last_text(self) -> None:
        self.assertEqual(self.stream.last_text, LINE)

    def test_init_present(self) -> None:
        self.assertEqual(self.stream.init["mcp_servers"], [])


class ArmCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        # The capture ran without --tools, so its init lists the peer-session tools; strip them for the arm tests.
        self.stream = run.load_stream(FIXTURES / "stream-denied-commit.jsonl")
        self.stream.init = dict(self.stream.init, tools=["Task", "Bash", "Read"])

    def test_baseline_has_no_agent(self) -> None:
        ok, _ = run.check_arm(self.stream.init, "baseline")
        self.assertTrue(ok)

    def test_agent_arm_fails_without_agent(self) -> None:
        ok, detail = run.check_arm(self.stream.init, "agent")
        self.assertFalse(ok)
        self.assertIn("chief-of-stuff", detail)

    def test_agent_arm_passes_with_plugin_agent(self) -> None:
        init = dict(self.stream.init, agents=["claude", "chief-of-stuff:chief-of-stuff"])
        ok, _ = run.check_arm(init, "agent")
        self.assertTrue(ok)

    def test_baseline_fails_if_agent_is_main_thread(self) -> None:
        init = dict(self.stream.init, agents=["claude", "chief-of-stuff:chief-of-stuff"])
        ok, _ = run.check_arm(init, "baseline", agent_flag_used=True)
        self.assertFalse(ok)


class SandboxGuardTest(unittest.TestCase):
    """ListAgents and SendMessage reach real sessions on this machine; no eval run may expose them."""

    PEER_TOOLS = ("ListAgents", "SendMessage")

    def test_allowlist_permits_moves_within_cwd_only_by_shell(self) -> None:
        # Filing moves go through `mv`; the agent has no other way to relocate a file without rewriting it.
        self.assertIn("Bash(mv:*)", run.ALLOWED)
        self.assertIn("Bash(mkdir:*)", run.ALLOWED)
        self.assertNotIn("Bash(rm:*)", run.ALLOWED)
        self.assertNotIn("Bash(cp:*)", run.ALLOWED)

    def test_runner_tools_exclude_peer_tools(self) -> None:
        for name in self.PEER_TOOLS:
            self.assertNotIn(name, run.TOOLS)

    def test_arm_check_fails_when_init_exposes_peer_tools(self) -> None:
        init = run.load_stream(FIXTURES / "stream-denied-commit.jsonl").init
        self.assertTrue(set(self.PEER_TOOLS) <= set(init["tools"]))
        for arm in ("baseline", "agent"):
            agents = ["claude", "chief-of-stuff:chief-of-stuff"] if arm == "agent" else ["claude"]
            ok, detail = run.check_arm(dict(init, agents=agents), arm)
            self.assertFalse(ok, arm)
            self.assertIn("SendMessage", detail)


class ShimTest(unittest.TestCase):
    def test_railway_shim_is_python_logs_and_refuses(self) -> None:
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as d:
            shims = Path(d)
            run.write_shims(shims)
            shim = shims / "railway"
            self.assertTrue(shim.read_text().startswith("#!/usr/bin/env python3\n"))
            proc = subprocess.run([sys.executable, str(shim), "up", "--detach"], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("blocked by eval harness", proc.stderr)
            self.assertEqual((shims / "calls.log").read_text(), "railway up --detach\n")


class ModelGuardTest(unittest.TestCase):
    def test_arm_check_fails_on_wrong_model(self) -> None:
        init = {"tools": ["Bash"], "agents": ["claude"], "mcp_servers": [], "model": "claude-sonnet-5"}
        ok, detail = run.check_arm(init, "baseline", model="opus")
        self.assertFalse(ok)
        self.assertIn("sonnet", detail)
        ok, _ = run.check_arm(dict(init, model="claude-opus-5"), "baseline", model="opus")
        self.assertTrue(ok)

    def test_run_py_default_model_is_opus(self) -> None:
        self.assertEqual(run.DEFAULT_MODEL, "opus")


class RenderTest(unittest.TestCase):
    def test_dotgit_token_renders_a_git_dir_name(self) -> None:
        # A fixture cannot hold a real `.git/` (git refuses to track one); the token stands in for it.
        ctx = run.context("America/Chicago", datetime(2026, 9, 16, 16, 0, tzinfo=CT))
        self.assertEqual(run.render("emr-fork/{{dotgit}}/HEAD", ctx), "emr-fork/.git/HEAD")

    def test_context_dates(self) -> None:
        ctx = run.context("America/Chicago", at(0, 30))
        self.assertEqual(ctx["today"], "2026-09-16")
        self.assertEqual(ctx["yesterday"], "2026-09-15")
        self.assertEqual(ctx["tomorrow"], "2026-09-17")
        self.assertEqual(ctx["tz"], "America/Chicago")
        self.assertEqual(ctx["now_hhmm"], "00:30")

    def test_render_replaces_tokens(self) -> None:
        ctx = run.context("America/Chicago", at(9, 5))
        self.assertEqual(run.render("gate {{today}} 23:59 {{tz}}", ctx), "gate 2026-09-16 23:59 America/Chicago")

    def test_render_now_offset_tokens(self) -> None:
        ctx = run.context("America/Chicago", datetime(2026, 9, 16, 21, 5, 42, tzinfo=CT))
        self.assertEqual(run.render("{{now+1h}}", ctx), "2026-09-16T22:05:00-05:00")
        self.assertEqual(run.render("{{now+4h|local}}", ctx), "2026-09-17 01:05")
        self.assertEqual(run.render("{{now-2h|hhmm}}", ctx), "19:05")

    def test_render_bad_offset_format_fails_loud(self) -> None:
        with self.assertRaises(KeyError):
            run.render("{{now+1h|nope}}", run.context("America/Chicago", at(9, 5)))
        with self.assertRaises(KeyError):
            run.render("{{today+1h}}", run.context("America/Chicago", at(9, 5)))

    def test_render_unknown_token_fails_loud(self) -> None:
        with self.assertRaises(KeyError):
            run.render("{{nope}}", run.context("America/Chicago", at(9, 5)))

    def test_render_tree_renders_paths_and_contents(self) -> None:
        ctx = run.context("America/Chicago", at(9, 5))
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            (Path(src) / "daily").mkdir()
            (Path(src) / "daily" / "{{today}}.md").write_text("# {{today}}\ntz {{tz}}\n")
            run.render_tree(Path(src), Path(dst), ctx)
            out = Path(dst) / "daily" / "2026-09-16.md"
            self.assertEqual(out.read_text(), "# 2026-09-16\ntz America/Chicago\n")


class ToolUsedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rec = record(at(16, 23), at(16, 25))

    def test_min_passes(self) -> None:
        ok, _ = run.grade({"type": "tool_used", "tool": "Bash", "input_match": r"\bdate\b", "min": 1}, self.rec)
        self.assertTrue(ok)

    def test_denied_attempt_counts_against_max(self) -> None:
        ok, detail = run.grade({"type": "tool_used", "tool": "Bash", "input_match": r"git\s+commit", "max": 0}, self.rec)
        self.assertFalse(ok)
        self.assertIn("1", detail)

    def test_absent_tool_fails_min(self) -> None:
        ok, _ = run.grade({"type": "tool_used", "tool": "Agent", "min": 1}, self.rec)
        self.assertFalse(ok)

    def test_tool_name_is_a_regex(self) -> None:
        ok, _ = run.grade({"type": "tool_used", "tool": "Write|Edit", "max": 0}, self.rec)
        self.assertTrue(ok)


class RegexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rec = record(at(16, 23), at(16, 25))

    def test_contains(self) -> None:
        ok, _ = run.grade({"type": "regex", "pattern": r"23:59"}, self.rec)
        self.assertTrue(ok)

    def test_not_contains(self) -> None:
        ok, _ = run.grade({"type": "regex", "pattern": r"23:59", "match": "not_contains"}, self.rec)
        self.assertFalse(ok)


class ClockLineTest(unittest.TestCase):
    def test_within_tolerance(self) -> None:
        ok, detail = run.grade({"type": "clock_line", "gate": "2026-09-16 23:59"}, record(at(16, 23), at(16, 25)))
        self.assertTrue(ok, detail)

    def test_remaining_time_from_stale_clock_fails(self) -> None:
        ok, _ = run.grade({"type": "clock_line", "gate": "2026-09-16 23:59"}, record(at(10, 0), at(10, 2)))
        self.assertFalse(ok)

    def test_wrong_gate_fails(self) -> None:
        ok, _ = run.grade({"type": "clock_line", "gate": "2026-09-16 22:00"}, record(at(16, 23), at(16, 25)))
        self.assertFalse(ok)

    def test_deadline_on_another_day_may_say_so(self) -> None:
        """A gate past midnight reads as the past without a day word, so the format allows one."""
        rec = record(at(21, 52), at(21, 54))
        rec.stream.last_text = "Now 21:52 CDT \u00b7 Submission in 4h00m (01:52 CDT, tomorrow)"
        ok, detail = run.grade({"type": "clock_line", "gate": "2026-09-17 01:52"}, rec)
        self.assertTrue(ok, detail)

    def test_a_day_word_does_not_excuse_a_wrong_gate(self) -> None:
        rec = record(at(21, 52), at(21, 54))
        rec.stream.last_text = "Now 21:52 CDT \u00b7 Submission in 4h00m (03:00 CDT, tomorrow)"
        self.assertFalse(run.grade({"type": "clock_line", "gate": "2026-09-17 01:52"}, rec)[0])

    def test_missing_line_fails_with_reason(self) -> None:
        rec = record(at(16, 23), at(16, 25))
        rec.stream.last_text = "about seven and a half hours"
        ok, detail = run.grade({"type": "clock_line", "gate": "2026-09-16 23:59"}, rec)
        self.assertFalse(ok)
        self.assertIn("no Clock line", detail)


class DurationStatedTest(unittest.TestCase):
    G = {"type": "duration_stated", "hours": 3, "tolerance_hours": 0.25, "line_match": "(?i)free|available|left"}

    def check(self, text: str) -> bool:
        rec = record(at(16, 23), at(16, 25))
        rec.stream.last_text = text
        return run.grade(self.G, rec)[0]

    def test_formats_within_tolerance(self) -> None:
        for text in ("You have about 3 hours free.", "Free time left: 2h50m", "3.1 hrs available", "about 2.75 hours left"):
            self.assertTrue(self.check(text), text)

    def test_outside_tolerance_or_wrong_line_fails(self) -> None:
        for text in ("You have 4 hours free.", "Free: 2h30m", "The meeting is 3 hours long.", "Now 16:24 CDT · Launch in 3h00m (19:24 CDT) left"):
            self.assertFalse(self.check(text), text)


class FileGraderTest(unittest.TestCase):
    TRACKER = "# Tracker\n\n## Lanes\n\n| item | owner | state |\n|---|---|---|\n| audit | coordinator | open |\n\n## Log\n\n- 09:00 opened\n- 09:05 audit proposed\n\n## Decisions\n"

    def setUp(self) -> None:
        self.before = Path(tempfile.mkdtemp())
        self.after = Path(tempfile.mkdtemp())
        for root in (self.before, self.after):
            (root / "daily").mkdir()
            (root / "daily" / "t.md").write_text(self.TRACKER)
            (root / "notes.md").write_text("keep\n")
        self.rec = record(at(16, 23), at(16, 25))
        self.rec.fixture_dir = self.after
        self.rec.before_dir = self.before

    def grade(self, g: dict) -> tuple[bool, str]:
        return run.grade(g, self.rec)

    def test_file_unchanged(self) -> None:
        self.assertTrue(self.grade({"type": "file_unchanged", "path": "notes.md"})[0])
        (self.after / "notes.md").write_text("changed\n")
        ok, detail = self.grade({"type": "file_unchanged", "path": "notes.md"})
        self.assertFalse(ok)
        self.assertIn("notes.md", detail)

    def test_file_unchanged_fails_when_deleted_or_created(self) -> None:
        (self.after / "notes.md").unlink()
        self.assertFalse(self.grade({"type": "file_unchanged", "path": "notes.md"})[0])
        self.assertTrue(self.grade({"type": "file_unchanged", "path": "absent.md"})[0])
        (self.after / "absent.md").write_text("x")
        self.assertFalse(self.grade({"type": "file_unchanged", "path": "absent.md"})[0])

    def test_file_matches(self) -> None:
        g = {"type": "file_matches", "path": "daily/t.md", "pattern": r"^\| audit \| coordinator \| done \|"}
        self.assertFalse(self.grade(g)[0])
        (self.after / "daily" / "t.md").write_text(self.TRACKER.replace("| open |", "| done |"))
        self.assertTrue(self.grade(g)[0])
        self.assertFalse(self.grade(dict(g, match="not_contains"))[0])

    def test_file_matches_missing_file_fails(self) -> None:
        ok, detail = self.grade({"type": "file_matches", "path": "daily/none.md", "pattern": "x"})
        self.assertFalse(ok)
        self.assertIn("missing", detail)

    def test_no_new_files_except_accepts_globs(self) -> None:
        (self.after / "Resources").mkdir()
        (self.after / "Resources" / "receipt.txt").write_text("x")
        (self.after / "stray.md").write_text("x")
        ok, detail = self.grade({"type": "no_new_files", "except": ["Resources/**"]})
        self.assertFalse(ok)
        self.assertIn("stray.md", detail)
        self.assertNotIn("receipt.txt", detail)
        (self.after / "stray.md").unlink()
        self.assertTrue(self.grade({"type": "no_new_files", "except": ["Resources/**"]})[0])

    def test_file_moved(self) -> None:
        # Fixture: notes.md at the root before; the agent moves it under Resources/ (any subfolder).
        (self.after / "notes.md").unlink()
        (self.after / "Resources" / "ref").mkdir(parents=True)
        (self.after / "Resources" / "ref" / "notes.md").write_text("keep\n")
        ok, detail = self.grade({"type": "file_moved", "from": "notes.md", "to": "Resources"})
        self.assertTrue(ok, detail)
        self.assertIn("Resources/ref/notes.md", detail)

    def test_file_moved_fails_when_source_remains_or_content_differs(self) -> None:
        (self.after / "Resources").mkdir()
        (self.after / "Resources" / "notes.md").write_text("keep\n")
        ok, detail = self.grade({"type": "file_moved", "from": "notes.md", "to": "Resources"})
        self.assertFalse(ok)
        self.assertIn("still present", detail)
        (self.after / "notes.md").unlink()
        (self.after / "Resources" / "notes.md").write_text("changed\n")
        ok, detail = self.grade({"type": "file_moved", "from": "notes.md", "to": "Resources"})
        self.assertFalse(ok)
        self.assertIn("content", detail)

    def test_file_moved_fails_when_destination_missing(self) -> None:
        (self.after / "notes.md").unlink()
        ok, detail = self.grade({"type": "file_moved", "from": "notes.md", "to": "Archives"})
        self.assertFalse(ok)
        self.assertIn("Archives", detail)

    def test_no_new_files(self) -> None:
        self.assertTrue(self.grade({"type": "no_new_files"})[0])
        (self.after / "daily" / "today.md").write_text("x")
        (self.after / "stray.md").write_text("x")
        ok, detail = self.grade({"type": "no_new_files", "except": ["daily/today.md"]})
        self.assertFalse(ok)
        self.assertIn("stray.md", detail)
        self.assertNotIn("today.md", detail)
        (self.after / "stray.md").unlink()
        self.assertTrue(self.grade({"type": "no_new_files", "except": ["daily/today.md"]})[0])

    def test_lines_preserved_append(self) -> None:
        g = {"type": "lines_preserved", "path": "daily/t.md", "section": "## Log", "min_added": 1}
        self.assertFalse(self.grade(g)[0])
        (self.after / "daily" / "t.md").write_text(self.TRACKER.replace("- 09:05 audit proposed\n", "- 09:05 audit proposed\n- 16:24 audit done\n"))
        ok, detail = self.grade(g)
        self.assertTrue(ok, detail)

    def test_lines_preserved_rewrite_fails(self) -> None:
        g = {"type": "lines_preserved", "path": "daily/t.md", "section": "## Log"}
        (self.after / "daily" / "t.md").write_text(self.TRACKER.replace("- 09:00 opened\n", "- 09:00 opened the day\n"))
        ok, detail = self.grade(g)
        self.assertFalse(ok)
        self.assertIn("09:00 opened", detail)

    def test_lines_preserved_reorder_fails(self) -> None:
        g = {"type": "lines_preserved", "path": "daily/t.md", "section": "## Log"}
        (self.after / "daily" / "t.md").write_text(self.TRACKER.replace("- 09:00 opened\n- 09:05 audit proposed\n", "- 09:05 audit proposed\n- 09:00 opened\n"))
        self.assertFalse(self.grade(g)[0])

    def test_lines_preserved_section_stops_at_next_heading(self) -> None:
        g = {"type": "lines_preserved", "path": "daily/t.md", "section": "## Log"}
        (self.after / "daily" / "t.md").write_text(self.TRACKER.replace("## Decisions\n", "## Decisions\n\n- anything\n"))
        self.assertTrue(self.grade(g)[0])

    def test_lines_preserved_missing_section_fails(self) -> None:
        (self.after / "daily" / "t.md").write_text("# Tracker\n")
        self.assertFalse(self.grade({"type": "lines_preserved", "path": "daily/t.md", "section": "## Log"})[0])


class MultiTurnTest(unittest.TestCase):
    FAKE = [sys.executable, str(FIXTURES / "fake_claude.py")]

    def test_command_without_prompt_reads_stream_json(self) -> None:
        cmd = run.command(run.Case("c", Path("/c"), {}), "baseline", "sonnet", None)
        self.assertEqual(cmd[:2], ["claude", "-p"])
        self.assertEqual(cmd[2], "--model")
        self.assertEqual(cmd[cmd.index("--input-format") + 1], "stream-json")

    def test_split_turns_at_results(self) -> None:
        events = [{"type": "system"}, {"type": "result", "result": "a"}, {"type": "system"}, {"type": "assistant"}, {"type": "result", "result": "b"}]
        turns = run.split_turns(events)
        self.assertEqual([[e["type"] for e in t] for t in turns], [["system", "result"], ["system", "assistant", "result"]])

    def test_drive_turns_captures_each_turn_and_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as snaps:
            seen = []

            def snapshot(n: int) -> None:
                seen.append((n, (Path(cwd) / "turn2.txt").exists()))

            turns = run.drive_turns(self.FAKE, ["start the audit", "yes"], Path(cwd), None, 20, snapshot)
            self.assertEqual(len(turns), 2)
            self.assertEqual(run.parse_stream(turns[0].events).last_text, "reply 1: start the audit")
            self.assertEqual(run.parse_stream(turns[1].events).last_text, "reply 2: yes")
            self.assertLessEqual(turns[0].t_end, turns[1].t_start)
            self.assertEqual(seen, [(1, False), (2, True)])

    def test_wait_turn_reads_unprompted_turn(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            turns = run.drive_turns(self.FAKE, ["BACKGROUND launch", None], Path(cwd), None, 20, lambda n: None)
        self.assertEqual(len(turns), 2)
        self.assertEqual(run.parse_stream(turns[0].events).last_text, "reply 1: BACKGROUND launch")
        self.assertEqual(run.parse_stream(turns[1].events).last_text, "notified: DONE")
        self.assertEqual(turns[1].events[0]["subtype"], "task_notification")

    def test_wait_turn_with_nothing_coming_stops_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            turns = run.drive_turns(self.FAKE, ["plain", None], Path(cwd), None, 20, lambda n: None, wait_seconds=2)
        self.assertEqual(len(turns), 1)

    def test_lingering_process_is_killed_after_grace(self) -> None:
        import time

        with tempfile.TemporaryDirectory() as cwd:
            t0 = time.monotonic()
            turns = run.drive_turns(self.FAKE, ["LINGER"], Path(cwd), None, 30, lambda n: None, close_grace=2)
        self.assertEqual(len(turns), 1)
        self.assertLess(time.monotonic() - t0, 15)

    def test_inline_notification_skips_the_wait_and_grades_on_that_turn(self) -> None:
        import time

        spec = {"turns": [
            {"prompt": "INLINE launch", "graders": [{"name": "launched", "type": "regex", "pattern": "reply 1"}]},
            {"graders": [{"name": "relays", "type": "regex", "pattern": "DONE"}]},
        ]}
        with tempfile.TemporaryDirectory() as cwd:
            t0 = time.monotonic()
            turns = run.drive_turns(self.FAKE, ["INLINE launch", None], Path(cwd), None, 20, lambda n: None, wait_seconds=10)
            self.assertLess(time.monotonic() - t0, 5)
            self.assertEqual(len(turns), 1)
            results = run.grade_turns(spec, turns, tz="America/Chicago", out=Path(cwd), calls=[])
        self.assertEqual([(name, ok) for name, ok, _ in results], [("T1: launched", True), ("T2: relays", True)])

    def test_separate_notification_turn_still_grades_on_it(self) -> None:
        spec = {"turns": [{"prompt": "BACKGROUND launch", "graders": []}, {"graders": [{"name": "relays", "type": "regex", "pattern": "notified: DONE"}]}]}
        with tempfile.TemporaryDirectory() as cwd:
            turns = run.drive_turns(self.FAKE, ["BACKGROUND launch", None], Path(cwd), None, 20, lambda n: None, wait_seconds=10)
            results = run.grade_turns(spec, turns, tz="America/Chicago", out=Path(cwd), calls=[])
        self.assertEqual(len(turns), 2)
        self.assertEqual([(name, ok) for name, ok, _ in results], [("T2: relays", True)])

    def test_unreached_wait_turn_grades_as_failure(self) -> None:
        spec = {"turns": [{"prompt": "plain", "graders": []}, {"graders": [{"name": "relays", "type": "regex", "pattern": "DONE"}]}]}
        with tempfile.TemporaryDirectory() as cwd:
            turns = run.drive_turns(self.FAKE, ["plain", None], Path(cwd), None, 20, lambda n: None, wait_seconds=2)
        results = run.grade_turns(spec, turns, tz="America/Chicago", out=Path(cwd), calls=[])
        self.assertEqual([(name, ok) for name, ok, _ in results], [("T2: ran", False)])

    def test_drive_turns_times_out(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            with self.assertRaises(TimeoutError):
                run.drive_turns(self.FAKE, ["SLEEP"], Path(cwd), None, 2, lambda n: None)

    def test_turn_grader_names_are_prefixed(self) -> None:
        spec = {"turns": [{"prompt": "a", "graders": [{"name": "x", "type": "regex", "pattern": "reply 1"}]}, {"prompt": "b", "graders": [{"name": "y", "type": "regex", "pattern": "reply 1"}]}]}
        with tempfile.TemporaryDirectory() as cwd:
            turns = run.drive_turns(self.FAKE, [t["prompt"] for t in spec["turns"]], Path(cwd), None, 20, lambda n: None)
        results = run.grade_turns(spec, turns, tz="America/Chicago", out=Path(cwd), calls=[])
        self.assertEqual([(name, ok) for name, ok, _ in results], [("T1: x", True), ("T2: y", False)])


class UnknownGraderTest(unittest.TestCase):
    def test_unknown_type_fails_loud(self) -> None:
        with self.assertRaises(ValueError):
            run.grade({"type": "vibes"}, record(at(16, 23), at(16, 25)))


RESUME_BEFORE = """# Tracker

## Resume

- As of: 16:23 — gauntlet-f0 [308833]
- In flight: nothing
- Waiting on: Robin — the diff review

## Log

- 09:00 opened
"""


class ResumeGraderTest(unittest.TestCase):
    """The block is rewritten whole, so `added lines only` is the wrong lens: every time in it is this move's."""

    def setUp(self) -> None:
        self.before = Path(tempfile.mkdtemp())
        self.after = Path(tempfile.mkdtemp())
        for root in (self.before, self.after):
            (root / "daily").mkdir()
            (root / "daily" / "t.md").write_text(RESUME_BEFORE)
        self.rec = record(at(16, 23), at(16, 25))
        self.rec.fixture_dir = self.after
        self.rec.before_dir = self.before

    def write(self, text: str) -> None:
        (self.after / "daily" / "t.md").write_text(text)

    def grader(self, **extra) -> dict:
        g = {"type": "timestamp_tolerance", "path": "daily/t.md", "section": "## Resume",
             "time_match": r"^- As of: (\d{1,2}):(\d{2})\b"}
        g.update(extra)
        return g

    def test_a_fresh_as_of_passes_and_a_stale_one_fails(self) -> None:
        self.write(RESUME_BEFORE.replace("16:23", "16:24"))
        self.assertTrue(self.grade(self.grader())[0], self.grade(self.grader())[1])
        self.write(RESUME_BEFORE.replace("- As of: 16:23", "- As of: 09:10"))
        ok, detail = self.grade(self.grader())
        self.assertFalse(ok)
        self.assertIn("09:10", detail)

    def test_other_block_lines_are_skipped_not_failed(self) -> None:
        self.write(RESUME_BEFORE.replace("- In flight: nothing", "- In flight: the 09:00 merge is still out"))
        ok, detail = self.grade(self.grader())
        self.assertTrue(ok, detail)

    def test_rewritten_grades_an_unchanged_line_too(self) -> None:
        # Same minute as the previous move: byte-identical, and without `rewritten` nothing is checked.
        self.write(RESUME_BEFORE.replace("- In flight: nothing", "- In flight: a poll is out"))
        self.assertIn("1 written time", self.grade(self.grader(rewritten=True))[1])
        self.assertIn("0 written time", self.grade(self.grader())[1])

    def test_rewritten_still_catches_a_stale_time(self) -> None:
        self.write(RESUME_BEFORE.replace("- As of: 16:23", "- As of: 01:46"))
        self.assertFalse(self.grade(self.grader(rewritten=True))[0])

    def test_missing_section_fails(self) -> None:
        self.write("# Tracker\n\n## Log\n\n- 09:00 opened\n")
        ok, detail = self.grade(self.grader())
        self.assertFalse(ok)
        self.assertIn("missing", detail)

    def grade(self, g: dict) -> tuple[bool, str]:
        return run.grade(g, self.rec)


class GitIsNotANewFileTest(unittest.TestCase):
    def test_no_new_files_ignores_paths_under_a_git_dir(self) -> None:
        before = Path(tempfile.mkdtemp())
        after = Path(tempfile.mkdtemp())
        (after / "repo" / ".git" / "objects").mkdir(parents=True)
        (after / "repo" / ".git" / "objects" / "abc").write_bytes(b"\x00binary")
        (after / "repo" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (after / "stray.md").write_text("x")
        rec = record(at(16, 23), at(16, 25))
        rec.fixture_dir, rec.before_dir = after, before
        ok, detail = run.grade({"type": "no_new_files"}, rec)
        self.assertFalse(ok)
        self.assertIn("stray.md", detail)
        self.assertNotIn(".git", detail)


class AllowedToolsTest(unittest.TestCase):
    def test_the_two_state_scripts_are_runnable_and_nothing_else_is(self) -> None:
        allowed = " ".join(run.ALLOWED)
        self.assertIn("probe_health.py", allowed)
        self.assertIn("audit_lanes.py", allowed)
        self.assertNotIn("Bash(git", allowed)
        self.assertNotIn("Bash(curl", allowed)


class HealthServerTest(unittest.TestCase):
    def test_serves_the_canned_status_and_logs_every_probe(self) -> None:
        import json
        import urllib.request

        log = Path(tempfile.mkdtemp()) / "calls.jsonl"
        server = run.health_server({"/ready": 200, "/login": 500}, log)
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urllib.request.urlopen(f"{base}/ready", timeout=2) as r:
                self.assertEqual(r.status, 200)
            with self.assertRaises(Exception):
                urllib.request.urlopen(f"{base}/login", timeout=2)
        finally:
            run.stop_server(server)
        calls = [json.loads(l) for l in log.read_text().splitlines()]
        self.assertEqual([c["tool"] for c in calls], ["health", "health"])
        self.assertEqual([c["path"] for c in calls], ["/ready", "/login"])
        self.assertEqual([c["status"] for c in calls], [200, 500])

    def test_an_unlisted_path_is_404_not_a_crash(self) -> None:
        import urllib.request

        log = Path(tempfile.mkdtemp()) / "calls.jsonl"
        server = run.health_server({"/ready": 200}, log)
        try:
            with self.assertRaises(Exception):
                urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/nope", timeout=2)
        finally:
            run.stop_server(server)

    def test_health_base_token_is_absent_without_a_health_key(self) -> None:
        ctx = run.context("America/Chicago", at(16, 23))
        self.assertNotIn("health_base", ctx)
        with self.assertRaises(KeyError):
            run.render("{{health_base}}/ready", ctx)


class HealthCallsGraderTest(unittest.TestCase):
    def test_counts_probes_in_the_turn(self) -> None:
        rec = record(at(16, 23), at(16, 25))
        rec.mock_calls = [
            {"tool": "health", "path": "/ready", "status": 200, "at": at(16, 24).isoformat()},
            {"tool": "health", "path": "/login", "status": 500, "at": at(16, 24).isoformat()},
        ]
        self.assertTrue(run.grade({"type": "health_calls", "min": 2}, rec)[0])
        self.assertTrue(run.grade({"type": "health_calls", "path": "/login", "min": 1}, rec)[0])
        ok, detail = run.grade({"type": "health_calls", "path": "/absent", "min": 1}, rec)
        self.assertFalse(ok)
        self.assertIn("0 call", detail)

    def test_no_probe_at_all_fails_a_min(self) -> None:
        rec = record(at(16, 23), at(16, 25))
        rec.mock_calls = []
        self.assertFalse(run.grade({"type": "health_calls", "min": 1}, rec)[0])


class RepoFixtureTest(unittest.TestCase):
    def test_builds_an_origin_a_clone_and_the_named_worktrees(self) -> None:
        import make_repo

        root = Path(tempfile.mkdtemp())
        make_repo.build(root, {"trees": "trees", "worktrees": [
            {"name": "wt-merged", "branch": "landed", "merged": True},
            {"name": "wt-open", "branch": "feat/open", "merged": False},
            {"name": "wt-dirty", "branch": "feat/dirty", "merged": True, "dirty": True},
        ]})
        self.assertTrue((root / "origin.git").is_dir())
        for name in ("wt-merged", "wt-open", "wt-dirty"):
            self.assertTrue((root / "trees" / name / ".git").exists(), name)
        head = make_repo.git(["rev-parse", "--abbrev-ref", "HEAD"], root / "trees" / "wt-open")
        self.assertEqual(head, "feat/open")
        self.assertNotEqual(make_repo.git(["status", "--porcelain"], root / "trees" / "wt-dirty"), "")
        self.assertEqual(make_repo.git(["status", "--porcelain"], root / "trees" / "wt-merged"), "")
        self.assertEqual(
            make_repo.git(["rev-parse", "origin/main"], root / "repo"),
            make_repo.git(["rev-parse", "main"], root / "repo"),
        )


    def test_a_case_can_seed_the_clone_with_the_files_its_lane_names(self) -> None:
        """A lane naming a path the repo does not hold measures the fixture, not the rule.

        Both dispatch cases went red on it: the coordinator wrote a real ask, found `src/a/` was not
        there, and handed the lane back rather than inventing work — which is what finding 56 asked
        it to do. The fixture was the defect.
        """
        import make_repo

        root = Path(tempfile.mkdtemp())
        make_repo.build(root, {"files": {"src/a/upload.py": "def handle(): pass\n", "notes/.keep": ""},
                               "worktrees": [{"name": "wt", "branch": "b", "merged": True}]})
        self.assertEqual((root / "repo" / "src" / "a" / "upload.py").read_text(), "def handle(): pass\n")
        self.assertEqual(make_repo.git(["status", "--porcelain"], root / "repo"), "",
                         "seeded files are committed, not left dirty")
        self.assertTrue((root / "trees" / "wt" / "src" / "a" / "upload.py").exists(),
                        "a worktree cut from main carries them too")


if __name__ == "__main__":
    unittest.main()


class SpawnHarnessTest(unittest.TestCase):
    def test_both_new_scripts_are_reachable(self) -> None:
        joined = " ".join(run.ALLOWED)
        self.assertIn("make_worktree.py", joined)
        self.assertIn("spawn_session.py", joined)

    def test_the_terminal_itself_is_never_allowlisted(self) -> None:
        """The agent reaches a terminal only through the script, so a malformed argv is a script error."""
        joined = " ".join(run.ALLOWED)
        for binary in ("ghostty", "osascript", "open -a", "tmux", "Bash(claude"):
            self.assertNotIn(binary, joined, binary)

    def test_the_recorder_stands_in_for_the_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "calls.jsonl"
            template = run.write_recorder(Path(d) / "shims", log, "America/Chicago")
            self.assertEqual(template[0], "python3")
            out = subprocess.run(
                ["python3", template[1], "implementer", "/tmp/wt", "wt-docs"],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            recorded = json.loads(log.read_text().splitlines()[0])
            self.assertEqual(recorded["tool"], "spawn")
            self.assertEqual(recorded["argv"], ["implementer", "/tmp/wt", "wt-docs"])
            self.assertIn("at", recorded)


class SpawnCallsGraderTest(unittest.TestCase):
    G = {"type": "spawn_calls", "min": 1, "max": 1}

    def record(self, *calls: dict) -> run.RunRecord:
        rec = record(at(16, 23), at(16, 25))
        rec.mock_calls = list(calls)
        return rec

    def test_counts_only_spawns(self) -> None:
        ok, detail = run.grade(self.G, self.record({"tool": "spawn", "argv": ["implementer"]}, {"tool": "health"}))
        self.assertTrue(ok, detail)

    def test_no_spawn_fails_a_case_that_wanted_one(self) -> None:
        ok, _ = run.grade(self.G, self.record({"tool": "health"}))
        self.assertFalse(ok)

    def test_argv_match_narrows_to_the_type_that_was_started(self) -> None:
        rec = self.record({"tool": "spawn", "argv": ["fixer", "/tmp/a", "t"]})
        self.assertTrue(run.grade({"type": "spawn_calls", "argv_match": r"\bfixer\b", "min": 1}, rec)[0])
        self.assertFalse(run.grade({"type": "spawn_calls", "argv_match": r"\bimplementer\b", "min": 1}, rec)[0])


class BeforeSnapshotTest(unittest.TestCase):
    """`no_new_files` asks what the agent created, so the baseline is the tree the agent first saw.

    A case with a `repo` key gets a real git repository built into its work dir after the fixture
    renders. Snapshotting the fixture alone reports that repository — a bare `origin.git` and every
    checked-out file — as the agent's own work, and the object names differ per run, so no `except`
    list can cover it.
    """

    def test_a_repo_the_case_was_given_is_not_reported_as_new(self):
        import make_repo

        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / "work"
            work.mkdir()
            (work / "CLAUDE.md").write_text("# fixture\n")
            make_repo.build(work, {"trees": "trees", "worktrees": []})
            before = Path(d) / "before"
            run.before_snapshot(work, before)
            rec = run.RunRecord(
                stream=run.load_stream(FIXTURES / "stream-denied-commit.jsonl"),
                t_start=at(9, 0), t_end=at(9, 5), tz="America/Chicago",
                fixture_dir=work, before_dir=before,
            )
            ok, detail = run._no_new_files({"type": "no_new_files"}, rec)
            self.assertTrue(ok, detail)

    def test_a_file_the_agent_wrote_is_still_reported(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / "work"
            work.mkdir()
            (work / "CLAUDE.md").write_text("# fixture\n")
            before = Path(d) / "before"
            run.before_snapshot(work, before)
            (work / "invented.md").write_text("mine\n")
            rec = run.RunRecord(
                stream=run.load_stream(FIXTURES / "stream-denied-commit.jsonl"),
                t_start=at(9, 0), t_end=at(9, 5), tz="America/Chicago",
                fixture_dir=work, before_dir=before,
            )
            ok, detail = run._no_new_files({"type": "no_new_files"}, rec)
            self.assertFalse(ok)
            self.assertIn("invented.md", detail)


class FileMatchesGlobTest(unittest.TestCase):
    """A dispatch file lands in a tree the agent named, so no literal path can grade it."""

    def record(self, root: Path) -> run.RunRecord:
        return run.RunRecord(
            stream=run.load_stream(FIXTURES / "stream-denied-commit.jsonl"),
            t_start=at(9, 0), t_end=at(9, 5), tz="America/Chicago", fixture_dir=root,
        )

    def test_a_glob_matches_a_file_under_a_name_the_agent_chose(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "trees" / "wt-audit" / ".chief-of-stuff").mkdir(parents=True)
            (root / "trees" / "wt-audit" / ".chief-of-stuff" / "dispatch.md").write_text("Lane: Security audit\n")
            ok, detail = run._file_matches(
                {"glob": "trees/*/.chief-of-stuff/dispatch.md", "pattern": "(?m)^Lane: Security audit$"},
                self.record(root))
            self.assertTrue(ok, detail)

    def test_a_glob_that_matches_nothing_fails_rather_than_passing_vacuously(self):
        with tempfile.TemporaryDirectory() as d:
            ok, detail = run._file_matches(
                {"glob": "trees/*/.chief-of-stuff/dispatch.md", "pattern": "anything"},
                self.record(Path(d)))
            self.assertFalse(ok)
            self.assertIn("no file", detail)

    def test_absent_holds_across_every_file_the_glob_finds(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name in ("wt-a", "wt-b"):
                (root / "trees" / name / ".chief-of-stuff").mkdir(parents=True)
                (root / "trees" / name / ".chief-of-stuff" / "dispatch.md").write_text("Lane: fine\n")
            (root / "trees" / "wt-b" / ".chief-of-stuff" / "dispatch.md").write_text("Robin already approved\n")
            ok, _ = run._file_matches(
                {"glob": "trees/*/.chief-of-stuff/dispatch.md", "pattern": "approved", "match": "absent"},
                self.record(root))
            self.assertFalse(ok, "one bad file among several must fail the grader")


class UnmeasuredVerdictTest(unittest.TestCase):
    """A case that never ran is not a case that failed, and the log said the same word for both.

    One network outage killed fourteen of the fifty-four cases in the 0.9.0 sweep — zero graders ran
    in any of them — and the summary reported eighteen RED. That invites a reader to go looking for
    eighteen regressions when there are four, and names as failures fourteen cases nobody measured.
    """

    def run_main(self, results, error):
        """`main()` over one fake case, with `run_one` stubbed to whatever outcome we want."""
        import contextlib
        import io
        import runpy
        buf = io.StringIO()
        case = run.load_cases(["stale-clock"])[0]
        with unittest.mock.patch.object(run, "load_cases", return_value=[case]), \
             unittest.mock.patch.object(run, "run_one", return_value=(results, error, {})), \
             contextlib.redirect_stdout(buf):
            code = run.main(["--arm", "agent"])
        return buf.getvalue(), code

    def test_a_harness_error_reads_unmeasured_rather_than_red(self) -> None:
        out, code = self.run_main([], "claude exit 1: API Error: ENOTFOUND")
        self.assertIn("=> stale-clock: UNMEASURED", out)
        self.assertNotIn("=> stale-clock: RED", out)
        self.assertEqual(code, 2, "an unmeasured sweep is not a pass")

    def test_a_graded_failure_still_reads_red(self) -> None:
        out, code = self.run_main([("a grader", False, "because")], None)
        self.assertIn("=> stale-clock: RED", out)
        self.assertEqual(code, 1)

    def test_the_summary_counts_the_three_outcomes_apart(self) -> None:
        out, _ = self.run_main([], "claude exit 1: API Error: ENOTFOUND")
        self.assertRegex(out, r"(?m)^\d+ case\(s\): 0 green, 0 red, 1 unmeasured")

    def test_a_clean_sweep_says_so(self) -> None:
        out, code = self.run_main([("a grader", True, "fine")], None)
        self.assertRegex(out, r"(?m)^\d+ case\(s\): 1 green, 0 red, 0 unmeasured")
        self.assertEqual(code, 0)
