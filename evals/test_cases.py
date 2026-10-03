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

    def test_large_tracker_graders_enforce_the_read_rule(self) -> None:
        """#46: Resume "reads it through `chief-of-stuff tracker` and never runs grep, sed, or tail on the tracker file".
        Two stored runs broke the rule and passed: a `cat` of the whole file, and a grep whose pattern held a `|`.
        Each command pins one branch of a pattern: with the branch removed, the command lands on the other side."""
        used, shell = (g["input_match"] for g in spec(EVALS / "cases" / "resume-reads-a-large-tracker")["graders"][:2])
        t = "daily/2026-10-03-tracker.md"
        n = Path(t).name

        def hit(pattern: str, command: str) -> bool:
            # The description is the model's prose about the call. It names the file and a tool here, and decides nothing.
            said = f"Show {t}; cat is not used (head of the file)"
            return re.search(pattern, json.dumps({"command": command, "description": said})) is not None

        for command in ("chief-of-stuff tracker section Resume", "chief-of-stuff tracker sections", "chief-of-stuff tracker tasks",
                        "chief-of-stuff tracker --root . --date 2026-10-03 tasks --not done", "chief-of-stuff tracker --root=. header",
                        "chief-of-stuff tracker --verbose header", "python3 /release/chief_of_stuff.py tracker header",
                        "python3 /release/scripts/tracker_read.py --root . sections"):
            self.assertTrue(hit(used, command), command)
        for command in ("chief-of-stuff tracker --help", f"chief-of-stuff tracker {t} sections", "chief-of-stuff tracker",
                        "chief-of-stuff tracker headers", "chief-of-stuff tracker --root . tasksfile",
                        "chief-of-stuff audit --date 2026-10-03; ls daily/"):
            self.assertFalse(hit(used, command), command)
        for command in (
                # each tool, first in the call
                f"cat {t}", f"grep -n '^## ' {t}", f"sed -n '10,40p' {t}", f"tail -50 {t}", f"head -8 {t}", f"cut -c1-160 {t}",
                f"awk -F'|' '{{print $2}}' {t}",
                # a `|` inside the pattern hides nothing
                f'grep -n "Coordinator:\\|^## Resume" {t}', f"grep -nE '^## |^- As of' {t}",
                # the file by name, or by a glob that can only be the tracker
                f"cd daily; grep -n '^## ' *-tracker*", "cd daily; grep -n '^## ' *tracker*", "cd daily && grep -n '^## ' 2026-10-03-tracker*",
                "cat daily/2026-10-03-tracker.m*", "cd daily; cat tracker.md",
                # command positions: after a separator, a group, a substitution, a negation, a line break
                f"cd daily && tail -5 {n}", f"wc -l {t} | cut -d' ' -f1", f"echo $(head -3 {t})", f"echo `head -3 {t}`",
                f"{{ head -3 {t}; echo; }}", f"! grep -q x {t}", f"date\nhead -3 {t}", f"date\n\thead -3 {t}",
                # after a shell word
                'for f in daily/*-tracker.md; do head -3 "$f"; done', f"if test -f {t}; then cat {t}; fi",
                f"if test -f notes.md; then true; else cat {t}; fi", f"if grep -q '^## Resume' {t}; then echo yes; fi",
                f"while grep -q waiting {t}; do sleep 1; done", f"until grep -q done {t}; do sleep 1; done",
                f"command cat {t}", f"env cat {t}", f"env LC_ALL=C cat {t}", f"time cat {t}", f"eval cat {t}", f"exec cat {t}",
                "find daily -name '*-tracker.md' -exec head -5 {} \\;",
                # through xargs, with and without flags
                "ls daily/*-tracker.md | xargs cat", f"echo {t} | xargs -n1 head -3",
                # an environment prefix, a quoted one, a path to the tool
                f"LC_ALL=C grep -c x {t}", f'FOO="a b" grep -c x {t}', f"FOO='a b' grep -c x {t}",
                f"TZ=America/Chicago date; /usr/bin/grep -c open {t}", f"F={t}; grep -n '^## ' $F"):
            self.assertTrue(hit(shell, command), command)
        for command in ("chief-of-stuff tracker section Log | tail -8", "chief-of-stuff tracker tasks --not done | head -20",
                        'chief-of-stuff log --root . "cut the tail of tracker.md prose"', f"ls -la {t}", f"wc -l {t}",
                        f"cats {t}", "grep -n TODO notes/plan.md", "cat daily/2026-10-03.md", "TZ=America/Chicago date"):
            self.assertFalse(hit(shell, command), command)

    def test_bad_setting_graders_read_the_health_call_and_the_named_key(self) -> None:
        """#47: "`health` names the key and exits nonzero". One grader finds the health call under each spelling the
        allowlist admits, in the call's command and nowhere else; the other finds `[workers] mode` in the reply however
        it is punctuated, and a reply that names only the file and the value is not one that names the key."""
        used, named = (g for g in spec(EVALS / "cases" / "open-day-names-a-bad-setting")["graders"])
        self.assertEqual((used["type"], used["tool"], used["min"]), ("tool_used", "Bash", 1))
        # run._regex searches the reply (`rec.stream.last_text`) and passes on a hit unless `match` says otherwise.
        self.assertEqual((named["type"], named.get("match", "contains")), ("regex", "contains"))

        def hit(command: str) -> bool:
            # The description is the model's prose about the call. It names the health call here, and decides nothing.
            said = "Before chief-of-stuff health runs (probe_health.py)"
            return re.search(used["input_match"], json.dumps({"command": command, "description": said})) is not None

        for command in ("chief-of-stuff health --config CLAUDE.md", "chief-of-stuff health", "chief_of_stuff health",
                        "python3 /release/chief_of_stuff.py health --config CLAUDE.md",
                        "python3 /release/scripts/probe_health.py --config CLAUDE.md",
                        "cd /tmp/ws; chief-of-stuff health --config CLAUDE.md 2>&1 | head -20",
                        'echo "opening"; chief-of-stuff health'):
            self.assertTrue(hit(command), command)
        for command in ("chief-of-stuff pages --ensure", "TZ=America/Chicago date", "chief-of-stuff audit --date 2026-10-03",
                        "chief-of-stuff healthcheck", "chief-of-stuff healthy --config CLAUDE.md", "chief-of-stuff health_all",
                        "python3 /release/chief_of_stuff.py healthz", "python3 /release/scripts/probe_healthXpy"):
            self.assertFalse(hit(command), command)
        for reply in ("`[workers] mode` must be `interactive` or `one-shot`", "workers.mode is invalid",
                      "the `mode` key under `[workers]` is not valid", "Settings: **[Workers] Mode** is wrong"):
            self.assertRegex(reply, named["pattern"])
        for reply in ("Settings look fine.", "All workers are idle.", "Dark mode is on.",
                      '`chief-of-stuff.toml` has `mode = "oneshot"`, not `one-shot`', "`chief-of-stuff.toml` is invalid.",
                      "The settings file `chief-of-stuff.toml` did not load: `oneshot` is not a value it takes."):
            self.assertNotRegex(reply, named["pattern"])

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
