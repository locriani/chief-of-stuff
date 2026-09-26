"""A lint over every eval case: the fixture shapes that rewarded the coordinator for doing less.

Four shapes in one day (findings 65, 68, 74): a repo with no files, a fixture with no `CLAUDE.md`, a
Sessions table the ruleset had stopped writing, a `- Verified:` line with no clock. Every one passed
every unit test, because no test read the cases. This one does, and it runs under CIMP on main.
"""

import json
import re
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

EVALS = Path(__file__).resolve().parent
sys.path.insert(0, str(EVALS))
sys.path.insert(0, str(EVALS.parent / "scripts"))
import run  # noqa: E402
import render_board as rb  # noqa: E402

CASES = sorted(p for p in (EVALS / "cases").iterdir() if (p / "case.json").exists())
SESSIONS_HEADER = "| ref | name | state | doing | waiting on | free at | constraints | children | last reply |"
# Both Tasks widths are legal: the renderer keys cells by the header and a six-column table is every task unsized.
TASKS_HEADERS = ("| item | owner | state | since | due | checklist |", "| item | owner | state | since | due | size | checklist |")
VERIFIED = re.compile(r"^- Verified \d{2}:\d{2}:")
# What `mock_peers.py` reads off a session row. Anything else is a premise the run never sees.
SESSION_KEYS = {"ref", "name", "state", "started_hours_ago", "started_minutes_ago"}


def spec(case: Path) -> dict:
    return json.loads((case / "case.json").read_text())


def graders(s: dict) -> list[dict]:
    out = list(s.get("graders", []))
    for turn in s.get("turns", []):
        out.extend(turn.get("graders", []))
    return out


def ctx(s: dict) -> dict[str, str]:
    c = run.context(s.get("tz", "America/Chicago"), datetime.now(ZoneInfo("UTC")))
    c["health_base"] = "http://127.0.0.1:0"
    return c


def fixture_files(case: Path, suffix: str) -> list[Path]:
    return sorted(p for p in (case / "fixture").rglob(f"*{suffix}") if p.is_file()) if (case / "fixture").exists() else []


class CaseLintTest(unittest.TestCase):
    def test_there_are_cases(self) -> None:
        self.assertGreater(len(CASES), 40)

    def test_one_shot_launch_grader_handles_quoted_task_arguments(self) -> None:
        case = EVALS / "cases" / "one-shot-dispatch-autonomous"
        pattern = spec(case)["graders"][0]["input_match"]
        for entry in ("python3 /release/chief_of_stuff.py worker", "chief-of-stuff worker",
                      "python3 /release/scripts/spawn_session.py"):
            command = entry + ' --task "Security audit" --runtime claude --dry-run'
            self.assertRegex(json.dumps({"command": command}), pattern)

    def test_every_grader_type_is_one_the_runner_dispatches(self) -> None:
        for case in CASES:
            for g in graders(spec(case)):
                self.assertIn(g.get("type"), run.GRADER_TYPES, f"{case.name}: grader {g.get('name')!r}")

    def test_the_grader_type_tuple_is_the_dispatcher(self) -> None:
        """A tuple beside the dispatcher drifts from it; this pins the two together."""
        with self.assertRaises(ValueError):
            run.grade({"type": "no_such_grader"}, None)
        self.assertNotIn("no_such_grader", run.GRADER_TYPES)

    def test_every_pattern_compiles_after_placeholders(self) -> None:
        for case in CASES:
            s = spec(case)
            for g in graders(s):
                if "pattern" in g:
                    try:
                        re.compile(run.render(g["pattern"], ctx(s)))
                    except (re.error, KeyError) as e:
                        self.fail(f"{case.name}: grader {g.get('name')!r}: {e}")

    def test_a_case_with_a_repo_has_a_coordinator_block(self) -> None:
        """Finding 74: with no `## Coordinator` block the coordinator correctly refuses to work, and the fixture passes when it does less."""
        for case in CASES:
            if "repo" in spec(case):
                self.assertTrue((case / "fixture" / "CLAUDE.md").exists(), f"{case.name}: repo but no fixture/CLAUDE.md")

    def test_a_repo_with_worktrees_has_files(self) -> None:
        """Finding 68: a tree with nothing in it rewards not looking; six cases had one."""
        for case in CASES:
            repo = spec(case).get("repo") or {}
            if repo.get("worktrees"):
                self.assertTrue(repo.get("files"), f"{case.name}: worktrees over an empty repo")

    def test_a_case_with_peers_has_sessions_the_mock_can_read(self) -> None:
        for case in CASES:
            s = spec(case)
            if "peers" not in s:
                continue
            sessions = case / s["peers"]
            self.assertTrue(sessions.exists(), f"{case.name}: peers names {s['peers']} which is missing")
            for row in json.loads(sessions.read_text()):
                unknown = set(row) - SESSION_KEYS
                self.assertFalse(unknown, f"{case.name}: session {row.get('name')!r} sets {sorted(unknown)}, which mock_peers.py never reads")

    def test_every_placeholder_is_one_the_runner_fills(self) -> None:
        for case in CASES:
            s = spec(case)
            c = ctx(s)
            for f in fixture_files(case, ""):
                try:
                    run.render(f.read_text(errors="replace"), c)
                except KeyError as e:
                    self.fail(f"{case.name}: {f.relative_to(case)}: unknown placeholder {e}")

    def test_fixture_sessions_tables_have_the_nine_columns(self) -> None:
        """Finding 65, third point: fixtures are anonymised copies of live shapes and drift from what the ruleset now asks for."""
        for case in CASES:
            for f in fixture_files(case, "tracker.md"):
                for line in f.read_text().splitlines():
                    if line.startswith("| ref |"):
                        self.assertEqual(line.strip(), SESSIONS_HEADER, f"{case.name}: {f.name}")

    def test_fixture_tasks_tables_have_a_header_the_renderer_keys_on(self) -> None:
        """0.11.0: the Tasks header decides whether a `size` cell exists; a header the renderer does not know reads every row as malformed."""
        for case in CASES:
            for f in fixture_files(case, "tracker.md"):
                for line in f.read_text().splitlines():
                    if line.startswith("| item |"):
                        self.assertIn(line.strip(), TASKS_HEADERS, f"{case.name}: {f.name}")

    def test_fixture_verified_lines_carry_a_clock_and_are_drawn(self) -> None:
        """Same finding: `- Verified:` with no time passed every test while the live tracker wrote `- Verified 16:52:`."""
        for case in CASES:
            s = spec(case)
            for f in fixture_files(case, "tracker.md"):
                text = run.render(f.read_text(), ctx(s))
                lines = [l for l in text.splitlines() if l.startswith("- Verified")]
                for line in lines:
                    self.assertRegex(line, VERIFIED, f"{case.name}: {f.name}: {line[:60]!r}")
                if lines:
                    self.assertIn("verified", rb.resume_fields(rb.parse_resume(text)), f"{case.name}: {f.name}: Verified parsed but not drawn")


if __name__ == "__main__":
    unittest.main()
