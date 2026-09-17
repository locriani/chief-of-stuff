"""Unit tests for the eval runner: stream parsing, fixture rendering, arm checks, graders.

The stream fixture is a real `claude -p --output-format stream-json --verbose` capture
(haiku, one denied `git commit`, one `date`), with machine paths stripped from `init`.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
