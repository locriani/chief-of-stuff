"""tracker_read.py (#46): `chief-of-stuff tracker` reads the tracker through the shared parser and writes nothing.

    chief-of-stuff tracker --root R [--date YYYY-MM-DD] header
    chief-of-stuff tracker --root R [--date YYYY-MM-DD] sections
    chief-of-stuff tracker --root R [--date YYYY-MM-DD] section <name>
    chief-of-stuff tracker --root R [--date YYYY-MM-DD] tasks [--not <state>]... [--state <state>]... [--issue <ref>]

The parser is the oracle: what a row means is `parse_tracker`'s to say, and these tests ask it rather than
restating it. The command is a presenter over it.
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import backlog  # noqa: E402
from _vendor.toon_format import decode as toon_decode  # noqa: E402
from md import section  # noqa: E402
from tracker import clip_name, parse_tracker  # noqa: E402
from workspace import read_config  # noqa: E402
import tracker_read  # noqa: E402

DAY = "2026-09-25"
HOST = "https://labs.example.test"
CLAUDE = ("# Workspace\n\n## Coordinator\n\n- User: Robin\n- Timezone: America/Chicago\n"
          "- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
          f"- Backlog: GitLab issues; host {HOST}; project team/app\n")
FIELDS = ["name", "owner", "state", "stage", "issue"]
# Each item cell is a paragraph carrying a word no other cell holds.
PROSE = ("It began as a one-line note and grew: the first attempt stalled on a missing fixture, the second landed "
         "half of it, and what is left is written here in full so that nobody has to ask again what was meant. ")
TRACKER = f"""# Tracker {DAY}

Coordinator: relay-12. Board: none.

## Resume

- As of: 09:40 — relay-12 [111111]
- In flight: nothing; the audit poll is out
- Next: read worker-a's reply
- Waiting on: Robin — the release notes owner
- Re-arm: the 30-minute check
- Verified 09:40: agent /ready 200 (09:38)

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Audit the upload endpoint | {PROSE}ITEMWORD1 closes it. | worker-a | running 09:30 | 09:00 |  | M | build | implement | #7 |  |
| Draft release notes | {PROSE}ITEMWORD2 closes it. | Robin | open | 09:00 |  | S | docs | plan | [#9]({HOST}/team/app/-/issues/9) |  |
|  | Rotate the staging credentials: {PROSE}ITEMWORD3 closes it. | unassigned |  | 09:05 |  |  |  |  |  |  |
| Migrate the nightly export job to the new queue and retire the old cron entry | {PROSE}ITEMWORD4 closes it. | worker-b | waiting | 09:10 |  | L | build | review | other/repo#7 |  |
| Report readiness | The endpoint reports `"ok"|"degraded"` and the flag reads a \\| b. {PROSE}ITEMWORD5 closes it. | worker-c | orphaned | 09:15 |  | M | build | fix | #11 |  |
| Ship the changelog | {PROSE}ITEMWORD6 closes it. | worker-a | done 08:10–08:55 | 08:00 |  | S | docs | main | #12 |  |

## Decisions

| time | item | Robin's words |
|---|---|---|

## Sessions

| ref | name | state | doing | waiting on | free at | constraints | children | last reply |
|---|---|---|---|---|---|---|---|---|
| a1b2c3 | worker-a | working | the upload audit | none | 11:00 | none | none | 09:30 |

## File ownership

- Audit the upload endpoint: worktree `wt-audit` · src/a/
- Report readiness: worktree `wt-ready` · src/b/ `x|y`

## Log

- 09:00 opened the day
- 09:30 audit assigned to worker-a
"""
HEADINGS = ["Resume", "Tasks", "Decisions", "Sessions", "File ownership", "Log"]
TASKS = parse_tracker(TRACKER).tasks


def names(tasks) -> list[str]:
    return sorted(clip_name(t.label) for t in tasks)


class TrackerReadTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "CLAUDE.md").write_text(CLAUDE)
        (self.root / "daily").mkdir()
        self.tracker = self.root / "daily" / f"{DAY}-tracker.md"
        self.tracker.write_text(TRACKER)

    def run_cli(self, *args: str, date: str | None = DAY) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        argv = ["--root", str(self.root), *(["--date", date] if date else []), *args]
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = tracker_read.main(argv)
            except SystemExit as stop:  # argparse refuses an argument by exiting
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def rows(self, *args: str) -> list[dict]:
        code, out, err = self.run_cli("tasks", *args)
        self.assertEqual(code, 0, err)
        return [] if out.strip() == "[]" else toon_decode(out)

    # Field report, 0.56.0: "three rounds of hand-rolled sed and awk. They found the sections, read the Resume block, and listed the open tasks"
    def test_sections_prints_the_headings_one_per_line(self):
        code, out, err = self.run_cli("sections")
        self.assertEqual(code, 0, err)
        self.assertEqual([line.removeprefix("## ").strip() for line in out.splitlines() if line.strip()], HEADINGS)

    # Field report, 0.56.0: "`chief-of-stuff tracker --section Resume` and `--tasks open` (and one for Sessions) would make each read one call"
    def test_section_prints_the_body_as_written(self):
        for name in ("Resume", "File ownership", "Sessions"):
            with self.subTest(section=name):
                code, out, err = self.run_cli("section", name)
                self.assertEqual(code, 0, err)
                self.assertEqual(out.strip("\n"), "\n".join(section(TRACKER, f"## {name}")).strip("\n"))

    # Eval finding on #46: the coordinator ran `head -8 <tracker>` for the lines above the first heading, which are no section.
    def test_header_prints_the_lines_above_the_first_heading_as_written(self):
        code, out, err = self.run_cli("header")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip("\n"), TRACKER.split("\n## ", 1)[0].strip("\n"))
        self.assertIn("Coordinator: relay-12. Board: none.", out)
        self.assertNotIn("## ", out)

    def test_header_of_a_tracker_that_starts_with_a_heading_is_empty(self):
        self.tracker.write_text(TRACKER[TRACKER.index("## Resume"):])
        code, out, err = self.run_cli("header")
        self.assertEqual((code, out.strip()), (0, ""), err)

    def test_a_section_that_does_not_exist_is_an_error_naming_it(self):
        code, out, err = self.run_cli("section", "Requirements")
        self.assertNotIn(code, (0, None))
        self.assertEqual(out.strip(), "")
        self.assertIn("Requirements", err)

    # Field evidence, 0.42.0: "147 non-done rows down to one line each: name, owner, state, stage, issue."
    def test_tasks_prints_exactly_the_five_fields(self):
        for row in self.rows():
            self.assertEqual(list(row), FIELDS)

    # Field evidence, 0.42.0: "The output has to be one line per task and must never print the item cell."
    def test_tasks_is_one_row_per_task(self):
        code, out, err = self.run_cli("tasks")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(toon_decode(out)), len(TASKS))
        # TOON spends one line on the header; no task takes a second line.
        self.assertLessEqual(len([line for line in out.splitlines() if line.strip()]), len(TASKS) + 1)

    # Field evidence, 0.42.0: "The output has to be one line per task and must never print the item cell."
    def test_the_item_cell_is_never_printed(self):
        _, out, _ = self.run_cli("tasks")
        self.assertNotIn("ITEMWORD", out)
        self.assertNotIn(PROSE.strip(), out)

    def test_name_is_the_label_clipped_by_clip_name(self):
        rows = self.rows()
        self.assertEqual(sorted(row["name"] for row in rows), names(TASKS))
        # One name is written over the cap and one row has no name cell: both are real names, neither the item.
        self.assertTrue(any(len(t.label) > len(clip_name(t.label)) for t in TASKS))
        self.assertTrue(any(not t.name.strip() for t in TASKS))

    # Third field report: "It breaks on any column reorder or a `|` inside a cell, and it bypasses the parser the scripts already share."
    def test_a_pipe_escaped_or_in_backticks_does_not_shift_columns(self):
        row = next(r for r in self.rows() if r["name"] == "Report readiness")
        self.assertEqual((row["owner"], row["state"], row["stage"], row["issue"]), ("worker-c", "orphaned", "fix", "#11"))

    def test_cells_are_printed_as_the_parser_read_them(self):
        row = next(r for r in self.rows() if r["name"] == "Audit the upload endpoint")
        self.assertEqual((row["owner"], row["stage"], row["issue"]), ("worker-a", "implement", "#7"))
        self.assertEqual(row["state"].split()[0], "running")

    # Third field report: "The coordinator listed non-done tasks with owner, state and issue by chaining two `awk` passes over the Tasks section"
    def test_not_leaves_out_tasks_of_that_state_kind(self):
        self.assertEqual(sorted(r["name"] for r in self.rows("--not", "done")),
                         names(t for t in TASKS if t.kind != "done"))

    def test_not_is_repeatable(self):
        self.assertEqual(sorted(r["name"] for r in self.rows("--not", "done", "--not", "running")),
                         names(t for t in TASKS if t.kind not in ("done", "running")))

    # Second field report: "It wanted the rows it could act on: open or unassigned, orphaned without a tree, and blocked. A `--state` filter on this read would cover that."
    def test_state_keeps_only_tasks_of_that_state_kind(self):
        for kind in ("open", "waiting", "orphaned", "running", "done"):
            with self.subTest(state=kind):
                wanted = [t for t in TASKS if t.kind == kind]
                self.assertTrue(wanted, "the fixture has a task of every kind")
                self.assertEqual(sorted(r["name"] for r in self.rows("--state", kind)), names(wanted))

    def test_state_is_repeatable(self):
        self.assertEqual(sorted(r["name"] for r in self.rows("--state", "open", "--state", "orphaned")),
                         names(t for t in TASKS if t.kind in ("open", "orphaned")))

    def test_a_blank_state_reads_as_open(self):
        blank = next(t for t in TASKS if not t.state.strip())
        self.assertIn(clip_name(blank.label), [r["name"] for r in self.rows("--state", "open")])
        self.assertNotIn(clip_name(blank.label), [r["name"] for r in self.rows("--not", "open")])

    def test_an_unknown_state_is_an_error_not_an_empty_list(self):
        for flag in ("--state", "--not"):
            with self.subTest(flag=flag):
                code, out, err = self.run_cli("tasks", flag, "blocked")
                self.assertNotIn(code, (0, None))
                self.assertEqual(out.strip(), "")
                self.assertIn("blocked", err)

    def test_an_empty_result_prints_an_empty_list(self):
        self.tracker.write_text(TRACKER.replace("| orphaned |", "| waiting |"))
        code, out, err = self.run_cli("tasks", "--state", "orphaned")
        self.assertEqual((code, out.strip()), (0, "[]"), err)

    # Field report, 21:26: "the coordinator ran grep on the tracker to find one Tasks row by its issue number."
    def test_issue_keeps_the_task_with_that_ref_in_every_form_kanban_accepts(self):
        home = read_config(self.root).backlog
        for arg in ("7", "#7", f"{HOST}/team/app/-/issues/7", "9", f"[#9]({HOST}/team/app/-/issues/9)", "other/repo#7"):
            with self.subTest(issue=arg):
                wanted = backlog.parse_issue_arg(arg, home)
                self.assertIsNotNone(wanted)
                # The ref decides, not the number: `#7` in the Backlog and `other/repo#7` are two issues.
                expected = [t for t in TASKS if t.issue.strip() and backlog.issue_ref(t.issue, home) == wanted]
                self.assertEqual(len(expected), 1)
                self.assertEqual([r["name"] for r in self.rows("--issue", arg)], names(expected))

    # Issue #46: "A read-only `chief-of-stuff tracker` command"
    def test_the_command_writes_nothing(self):
        before = (self.tracker.read_bytes(), self.tracker.stat().st_mtime_ns)
        files = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        for args in (("header",), ("sections",), ("section", "Resume"), ("section", "Nope"), ("tasks",),
                     ("tasks", "--not", "done"), ("tasks", "--state", "open"), ("tasks", "--issue", "#7")):
            self.run_cli(*args)
        self.assertEqual((self.tracker.read_bytes(), self.tracker.stat().st_mtime_ns), before)
        self.assertEqual(sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")), files)

    def test_date_defaults_to_today_in_the_workspace_zone(self):
        today = datetime.now(read_config(self.root).zone).date().isoformat()
        self.tracker.rename(self.tracker.with_name(f"{today}-tracker.md"))
        code, out, err = self.run_cli("section", "Log", date=None)
        self.assertEqual(code, 0, err)
        self.assertIn("opened the day", out)


if __name__ == "__main__":
    unittest.main()
