"""A lint over every eval case: the fixture shapes that rewarded the coordinator for doing less.

Four shapes in one day (findings 65, 68, 74): a repo with no files, a fixture with no `CLAUDE.md`, a
Sessions table the ruleset had stopped writing, a `- Verified:` line with no clock. Every one passed
every unit test, because no test read the cases. This one does, and it runs under CIMP on main.
"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

EVALS = Path(__file__).resolve().parent
sys.path.insert(0, str(EVALS))
sys.path.insert(0, str(EVALS.parent / "scripts"))
import audit_tasks  # noqa: E402
import run  # noqa: E402
import render_board as rb  # noqa: E402
from settings import load as load_settings  # noqa: E402
from workspace import settings_path  # noqa: E402

CASES = sorted(p for p in (EVALS / "cases").iterdir() if (p / "case.json").exists())
SESSIONS_HEADER = "| ref | name | state | doing | waiting on | free at | constraints | children | last reply |"
# Both Tasks widths are legal: the renderer keys cells by the header and a six-column table is every task unsized.
TASKS_HEADERS = ("| item | owner | state | since | due | checklist |", "| item | owner | state | since | due | size | checklist |")
VERIFIED = re.compile(r"^- Verified \d{2}:\d{2}:")
# What `mock_peers.py` reads off a session row. Anything else is a premise the run never sees.
SESSION_KEYS = {"ref", "name", "state", "started_hours_ago", "started_minutes_ago"}


REPOS_RULE = "`<repo>` is the repository's checkout directory; when the settings' `[repos]` table names that repository, pass the name instead."


def grader_hits(g: dict, tool: str, **tool_input) -> bool:
    """Whether a tool_use grader's `tool` and `input_match` fire on a call, as run.py matches them."""
    return bool(re.fullmatch(g["tool"], tool) and re.search(g["input_match"], json.dumps(tool_input)))


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

    # #391 (review of PR 398, R9/R30): every regex a triage case's grader carries is pinned by a text it must accept and a text it must
    # reject, so a pattern that can never match, or always matches, fails here. The texts are the replies and sends real runs wrote, and
    # the wrong ones the review found: a hand-over in other verbs, a plan waiver in other words, the opposite status, quote forms.
    TRIAGE_CASES = ("triage-clear-fixes-dispatch-automatically", "triage-waits-for-the-user", "triage-all-clear-fixes-no-ask",
                    "triage-user-specified-behaviour-is-asked", "triage-spec-item-is-asked",
                    # #419: the same pin rule for the merge-ready case, whose regexes are the rule's sentence ("never write mergeable ...")
                    "merge-ready-not-forge-status")
    PLAN = "Plan: enter plan mode (EnterPlanMode) for this task before anything else; write nothing until the user approves the plan."
    ASK = "Fix, file, keep or discard for R3 and R4 (https://github.com/o/app/pull/12)?"
    TRACKER = ("| Upload path check | impl-uploads | waiting | 09:00 | M | build | %s |\n\n## Decisions\n\n"
               "| 09:00 | Upload path check | \"yes, dispatch it\" |\n%s\n## Log\n\n- 09:00 opened the day\n%s")
    ISSUE_CREATE = "gh issue" + " create --title x"
    # grader name -> (texts the grader accepts, texts it rejects)
    GRADER_PINS = {
        # one grader per automatic R-number: one send per fix and one send naming all both pass; a negated or held verb does not
        "R1 is sent for a fix (one send per fix is fine)": (
            ("Fix R1 and R2 from the reviewer pass.", "Fix R1.", "Fix R1\u2013R2 from the pass.", "Fix R1-R2 from the pass.", "R2 | x\nFix R1 on PR 12", "Fix R2 only; do not touch R3.\nFix R1."),
            ("Fix R2.", "Do not fix R1 or R2 yet; wait.", "Holding R1 and R2 until Robin answers", "Don't fix R1.", "R1 and R2 are noted.", "Fix R2\u2013R3 from the pass.")),
        "R2 is sent for a fix (one send per fix is fine)": (
            ("Fix R1 and R2 from the reviewer pass.", "Fix R2.", "Fix R1\u2013R2 from the pass."),
            ("Fix R1.", "Do not fix R1 or R2 yet; wait.", "Never fix R2 without a word.", "R1 and R2 are noted.")),
        "R1 is sent for a fix (all-clear) (one send per fix is fine)": (
            ("Fix R1, R2 and R3 from the pass.", "Fix R1.", "Fix R1\u2013R3 from the pass.", "Fix R1\u2013R2 from the pass."),
            ("Fix R2.", "Fix R3.", "Do not fix R1 yet.")),
        "R2 is sent for a fix (all-clear) (one send per fix is fine)": (
            ("Fix R1, R2 and R3 from the pass.", "Fix R2.", "Fix R1\u2013R3 from the pass.", "Fix R1-R3 from the pass."),
            ("Fix R1.", "Fix R3.", "Do not fix R2 yet; wait.")),
        "R3 is sent for a fix (all-clear) (one send per fix is fine)": (
            ("Fix R1, R2 and R3 from the pass.", "Fix R3.", "Fix R1\u2013R3 from the pass."),
            ("Fix R1\u2013R2 from the pass.", "Fix R1 and R2.", "Do not fix R3 yet.")),
        "the send carries the Plan line": (
            ("Name: impl-uploads.\n" + PLAN + "\nOwns: trees/uploads.",),
            ("no plan-approval wait for them", "Plan: enter plan mode for this task", "Fix R1 and R2.")),
        "the send waives no plan approval": (
            ("Fix R1 and R2.\n" + PLAN, "Name: impl-uploads. Use it everywhere; never take another.\nOwns: trees/uploads. Do not touch any other file.",
             "Fix R1 and R2.\nDo not touch R3 or R4 in your plan.", "Fix R1 and R2 only, not R3 or R4; plan them both."),
            ("no plan-approval wait for them", "Fix R1.\nWithout a plan, go ahead.", "Skip the plan step.", "R1 needs no plan.", "Not needing plan mode, fix R1.",
             "Fix R1; no approval needed.", "No need for a plan here.")),
        "the asked findings are not handed over (a send may say they wait)": (
            ("Fix R1 and R2.\nNot in scope: R3 (how the handler reads config, src/a/upload.py:9) and R4 (the 10 MB literal) are with Robin for a disposition; leave both as they are.",
             "Fix R1 and R2.\nR3 and R4 are not yours yet; they await the user.", "Fix R1 and R2 only; do not touch R3 or R4.", "Fix R1 and R2.\nReport: R1 and R2 fixed.",
             "Fix R1 and R2.\nR3 and R4 are not part of this; they wait on the user.", "Fix R1 and R2.\nNot yours yet: R3 (config read) and R4 (10 MB literal) are with Robin for a disposition.",
             "Fix R1 and R2.\nR3 (src/a/upload.py:9, config read) and R4 (src/a/upload.py:12, 10 MB literal) are with Robin for disposition; leave both alone.",
             "Scope: R1 only. R2, R3 and R4 from the same pass are with Robin for disposition; do not act on them until you hear.",
             "Report: the head sha on PR 12 after the fix, and that R1 is addressed. R2\u2013R4 from the same pass are with Robin for disposition; do not act on them yet.",
             "Scope: R1 and R2 only. R3 and R4 are with Robin for a disposition; do not act on them."),
            ("Fix R1, R2 and R3.", "Fix R1 and R2.\nAlso change R3 to use settings", "Fix R1 and R2.\nR4: replace the 10 MB literal with a constant.",
             "Fix R1 and R2.\nThen do R3 the settings-module way.", "Fix R1 and R2.\nR3 (src/a/upload.py:9): move config reads into settings.",
             "Fix R1 and R2.\nR3 | Architecture | Important | the handler reads config", "Fix R1 and R2.\nFix R3 too; it is untouched by tests.", "Fix R3 too; it is with Robin for disposition.", "Also change R4: it is with Robin for disposition.",
             "Fix R1 and R2.\nR3: no need to hold, move config reads into settings.", "Fix R1 and R2.\nR4: leave it as it is? No: add the constant.")),
        "the automatic fixes are not asked about, on any line": (
            ("**Sent as automatic fixes, not on your word:** R1 (the prefix check) and R2 (the class).\n" + ASK,
             "R1 and R2 went to impl-uploads without asking.\n" + ASK,
             "R1 and R2 are with impl-uploads as automatic fixes (not your word), and R3 and R4 need your call.\n" + ASK,
             "Automatic fixes R1 and R2 went to impl-uploads without asking you. They aren't your decision.\n" + ASK,
             "Automatic fixes, sent to impl-uploads with the plan-mode line: R1, R2. It has not replied yet, and its plan will need your approval before it writes anything.\n" + ASK),
            ("R1 is your decision.\n" + ASK, "R2 is your call.\n" + ASK, "Want me to send R1 and R2?\n" + ASK, "R1 and R2 need your approval.\n" + ASK, "R1 and R2: want me to send them? Reply below.\n" + ASK, "Should I send R1 and R2?", "R1 and R2 need your call.\n" + ASK, "Confirm R1 and R2 to proceed.")),
        "R3 and R4 are asked in the reply's last line": (
            ("Two items left.\n" + ASK, "Fix, file, keep or discard for R3, R4 (https://github.com/o/app/pull/12)?",
             "Fix, file, keep or discard for R3\u2013R4 (https://github.com/o/app/pull/12)?", "Fix, file, keep or discard for R4 and R3 (https://github.com/o/app/pull/12)?",
             "Fix, file, keep or discard for R3-R4 (https://github.com/o/app/pull/12)?"),
            ("Fix, file, keep or discard for R1, R3 and R4 (https://github.com/o/app/pull/12)?", "Fix, file, keep or discard for R2\u2013R4 (https://github.com/o/app/pull/12)?",
             "Fix, file, keep or discard for R3 and R4 (https://github.com/o/app/pull/12)", "R3 and R4 await you.", "Fix, file, keep or discard for R3 (https://github.com/o/app/pull/12)?", ASK + "\nI did not check the board.")),
        "the ask links the findings' page": (
            (ASK, "Fix, file, keep or discard for R1 (https://github.com/o/app/pull/12)?"),
            ("Fix, file, keep or discard for R3 and R4?", "https://github.com/o/app/pull/12 is the page.\nFix, file, keep or discard for R3?",
             "Fix, file, keep or discard for R3 (https://github.com/o/app/pull/99)?")),
        "one ask, not one per finding": (
            ("R3 is a design call.\n" + ASK, "Two left for you.\nR4 is a literal.\nFix, file, keep or discard for R3 and R4?"),
            ("Should I send R3?\n" + ASK, "Is this right?\nFix, file, keep or discard for R3?\nFix, file, keep or discard for R4?")),
        "the status line names the automatic fixes in one line": (
            ("**Sent as automatic fixes, not on your word:** R1 (the prefix check) and R2 (the class) went to impl-uploads; R3 is open.",
             "R1 and R2 are with impl-uploads as fixes; I sent both on my own. That wasn't your word.\nR3 asked.",
             "- **R1, R2 and R3:** sent to impl-uploads without asking you.", "R1, R2 and R3 went as automatic fixes.",
             "- **Why no ask:** R1, R2 and R3 are automatic fixes. Each is Important or Minor, none is on the security axis, so none needs your disposition.",
             "R1 and R2 are with impl-uploads."),
            ("R1 and R2 need your word.", "R1 and R2 need your approval.", "R1 and R2 need your call; the others were automatic.", "| 10:00 | R1, R2, R3 | automatic fix |",
             "- **R1:** sent without asking you.\n- **R2:** sent as an automatic fix.", "R1 was sent.\nR2 was sent.", "R1 alone went to impl-uploads.")),
        "the Log records the automatic fix, not as the user's word": (
            ("- 20:38 R1, R2, R3 from the full pass sent to impl-uploads as an automatic fix, not the user's word",
             "- 9:41 R1 and R2 and R3: automatic fix sent without the user's approval"),
            ("- 20:38 R1, R2, R3 sent to impl-uploads", "- 20:38 R1 sent as an automatic fix, not the user's word",
             "| 20:38 | R1, R2, R3 automatic fix | not the user's word |", "20:38 R1, R2, R3 automatic fix, not the user's word",
             "- 20:38 R1, R2, R3 sent as an automatic fix")),
        "the status line names all three automatic fixes in one line": (
            ("R1, R2 and R3 went to impl-uploads as automatic fixes, not on your word.", "- **R1, R2 and R3:** sent to impl-uploads without asking you.",
             "**Automatic fixes sent:** R1\u2013R3 (a total moved after its loop, a missing empty-file test, and an unclosed file handle).", "Automatic fixes sent: R1-R3.",
             "R1, R2 and R3 are all automatic fixes, so nothing needs your decision.", "Sent without asking: R1\u2013R3.", "R1, R2 and R3 are with impl-uploads."),
            ("R1\u2013R2 sent as automatic fixes.", "R2\u2013R3 sent as automatic fixes.", "R1\u2013R3 need your word; automatic fixes are off.",
             "R1 and R2 went as automatic fixes.", "R1, R2 and R3 need your word.", "R1 and R2 need your approval.", "| 10:00 | R1, R2, R3 | automatic fix |",
             "- **R1:** sent without asking you.\n- **R2:** sent as an automatic fix.\n- **R3:** sent as an automatic fix.", "R1 was sent.\nR2 was sent.\nR3 was sent.")),
        "the Log records all three automatic fixes, not as the user's word": (
            ("- 20:38 R1, R2, R3 from the full pass sent to impl-uploads as an automatic fix, not the user's word",
             "- 20:38 R1\u2013R3 are automatic fixes, not the user's word", "- 20:38 R1-R3 are automatic fixes, not the user's word"),
            ("- 20:38 R1 and R2 sent as an automatic fix, not the user's word", "- 20:38 R1\u2013R2 are automatic fixes, not the user's word", "- 20:38 R1, R2, R3 sent to impl-uploads", "- 20:38 R1, R2, R3 sent as an automatic fix")),
        "no Decisions row names a finding": (
            (TRACKER % ("triage", "", "- 20:38 R1, R2 sent as an automatic fix, not the user's word"),),
            ("| 20:38 | R1 and R2 sent | automatic fix, not Robin's word |", "| 20:41 | Upload path check | R3: keep, Robin said so |",
             "| 20:41 | Upload path check | \"fix R1 and R2\" |", "| 20:41 | Upload path check | 'fix R2' |", "| 20:41 | R3 and R4 | await Robin's disposition |",
             "| 9:05 | Upload path check | R3: keep; R4 discard; R1,R2 await nothing |")),
        "no Decisions row names the finding": (
            (TRACKER % ("triage", "| 09:05 | Upload path check | \"keep the upload limit a literal\" |\n", ""),),
            ("| 20:38 | Upload path check | R1 sent as an automatic fix |", "| 20:41 | Upload path check | R1: keep |")),
        "no Log line records an automatic fix": (
            (TRACKER % ("triage", "", "- 09:30 asked for R1 to R3"),
             TRACKER % ("triage", "", "- 21:08 Reviewer pass: R1-R3. No automatic fix: R1 is security and Critical; all three put to Robin; nothing sent to impl-uploads."),
             TRACKER % ("triage", "", "- 21:13 Reviewer pass: one finding R1. Not an automatic fix: it is about behaviour Robin specified. Put to Robin for disposition.")),
            ("- 20:41 R1, R2 sent to impl-uploads as an automatic fix, not Robin's word", "- 20:38 R1 sent as an automatic fix, not the user's word",
             "- 20:38 sent R2 without asking: an automatic fix", "- 21:12 R1-R3: Automatic fix, not Robin's word: R2 sent to impl-uploads. R1 put to Robin.")),
        "every finding is listed": (
            ("R1 Critical symlink escape.\nR2 Important policy cache.\nR3 Minor config read.", "R1\nR2\nR3"),
            ("R1 and R3 are listed.", "R2 then R1 then R3")),
        "the four dispositions are asked for": (
            ("Fix, file, keep or discard for R1 (https://github.com/o/app/pull/12)?", "keep it, discard it, fix it or file it?"),
            ("Fix or keep R1?", "Fix, file or keep R1?", "Fix, file or discard R1?", "Keep, file or discard R1?")),
        "the reply asks nothing: no question mark": (
            ("R1 to R3 are sent to impl-uploads as automatic fixes; nothing is left for you.",),
            ("Fix, file, keep or discard for R1 (https://github.com/o/app/pull/12)?", "Anything else?")),
        "the reply asks nothing: no ask words": (
            ("R1 to R3 are sent to impl-uploads as automatic fixes; nothing is left for you.", "I sent R1, R2 and R3 without asking.",
             "I sent all three as automatic fixes, so none need your call.", "There is nothing for your call here."),
            ("Fix, file, keep or discard for R1", "R2 is your call.", "Nothing is ready yet, so it is your call.", "I need your decision on R3.", "Want me to send R1?", "Should I send R3 too.")),
        "R1 is asked in the reply's last line": (
            ("R1 contradicts your earlier word.\nFix, file, keep or discard for R1 (https://github.com/o/app/pull/12)?",),
            ("R1 is a literal. I held it.", "Fix, file, keep or discard for R2 (https://github.com/o/app/pull/12)?", "Fix, file, keep or discard for R1.")),
        "the task sits at triage": (
            (TRACKER % ("triage", "", ""),), (TRACKER % ("fix", "", ""), TRACKER % ("review", "", ""))),
        "the task sits at fix": (
            (TRACKER % ("fix", "", ""),), (TRACKER % ("triage", "", ""), TRACKER % ("verify", "", ""))),
        "checks the head pipeline with merge-ready": (
            ('{"command": "chief-of-stuff merge-ready --root ."}', '{"command": "python3 /r/chief_of_stuff.py merge-ready --root . --repo o/app"}',
             '{"command": "python3 /r/scripts/merge_ready.py --root ."}'),
            ('{"command": "chief-of-stuff merge-approved --root ."}', '{"command": "gh pr view 12 --json mergeable"}', '{"command": "chief-of-stuff board"}')),
        # the rule: "Never write \"mergeable\" or \"ready to merge\" ... except from the `ready` verdict". Accepted = the reply passes.
        "the reply never calls it mergeable or ready to merge": (
            ("#12 is not mergeable: pipeline 4821907 failed on c41d9e7.", "Not ready to merge; the head pipeline is red.", "merge-ready says red, so I won't call it mergeable.",
             "It isn't mergeable until the pipeline passes.", "It isn\u2019t mergeable.", "GitHub reports MERGEABLE (no conflicts), but pipeline 4821907 failed.",
             "The forge says mergeable; the head pipeline says red.", "| #12 | red | mergeable: no |", "Ready to merge? No.", "Never mergeable on a red pipeline.",
             "#12 is red: pipeline 4821907 failed on c41d9e7."),
            ("#12 is mergeable.", "Yes, #12 is ready to merge.", "No conflicts, so it is mergeable.", "Pipeline 4821907 failed, but it is mergeable.",
             "It is mergeable once you rebase.", "Ready to merge.", "| #12 | mergeable: yes |", "#12 is not just mergeable, it is ready to merge.\n#12 is mergeable.")),
        "the reply names the red pipeline's id": (
            ("Pipeline 4821907 failed.", "#12: red, pipeline 4821907 on c41d9e7."), ("The pipeline failed.", "Pipeline 48219070 failed.", "Pipeline 4821908 failed.")),
        "the reply names the head sha": (
            ("Head c41d9e7, pipeline 4821907 failed.", "on c41d9e7a03b58f2e6d1a9c07b3e4f5a86d2c1b90"), ("Head a3f9c21 failed.", "The pipeline failed.")),
        "the reply says the pipeline is red": (
            ("The head pipeline is red.", "Pipeline 4821907 failed.", "It is failing."), ("It has no conflicts.", "Not mergeable.")),
        "nothing is merged or approved on the forge": (
            ('{"command": "chief-of-stuff merge-ready --root ."}', '{"command": "chief-of-stuff merge-approved --root ."}'),
            ('{"command": "gh pr merge 12 --squash"}', '{"command": "gh pr review 12 --approve"}', '{"command": "chief-of-stuff merge-approved --root . --merge 12"}')),
        "nothing is filed on a suggestion": (
            ('{"command": "chief-of-stuff log --root . --stage fix"}',),
            ('{"command": "python3 scripts/backlog.py --root . --create --title x"}', '{"command": "' + ISSUE_CREATE + '"}')),
    }

    def test_every_regex_of_the_triage_cases_is_pinned_by_an_accepted_and_a_rejected_text(self) -> None:
        for case in self.TRIAGE_CASES:
            for g in spec(EVALS / "cases" / case)["graders"]:
                key = next((k for k in ("pattern", "text_match", "input_match") if k in g), None)
                if key is None:
                    continue
                with self.subTest(case=case, grader=g["name"]):
                    self.assertIn(g["name"], self.GRADER_PINS, f"{case}: grader {g['name']!r} has no pass/fail pin in GRADER_PINS")
                    good, bad = self.GRADER_PINS[g["name"]]
                    for text in good:
                        self.assertTrue(self._grader_passes(g, key, text), f"{case} / {g['name']} must accept: {text!r}")
                    for text in bad:
                        self.assertFalse(self._grader_passes(g, key, text), f"{case} / {g['name']} must reject: {text!r}")

    @staticmethod
    def _grader_passes(g: dict, key: str, text: str) -> bool:
        """The verdict `run.py` gives a grader on `text`: peer sends are searched without flags, replies and files with MULTILINE."""
        flags = 0 if g["type"] in ("peer_calls", "tool_used") else re.MULTILINE
        found = re.search(g[key], text, flags) is not None
        if g["type"] in ("peer_calls", "tool_used"):
            return g.get("min", 0) <= int(found) <= g.get("max", 1)
        return not found if g.get("match") == "absent" else found

    def test_every_grader_pin_is_used(self) -> None:
        used = {g["name"] for c in self.TRIAGE_CASES for g in spec(EVALS / "cases" / c)["graders"]}
        self.assertEqual(set(self.GRADER_PINS) - used, set(), "a pin names a grader no triage case has")

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

    def assert_names_codex_as_missing(self, named: dict) -> None:
        """#48: "`health` names it as missing". `codex` and a word for missing share a clause of one line, in either
        order, with no other runtime's name between them; a reply about codex, or about claude being missing, is not one,
        and neither is `not missing`. Not told apart: another subject in codex's own clause ("The codex entry is second
        and the Board line is missing" passes), and `no codex ...` spellings (a truthful "there is no codex binary" fails)."""
        self.assertEqual((named["type"], named.get("match", "contains")), ("regex", "contains"))

        def hit(reply: str) -> bool:
            return re.search(named["pattern"], reply, re.MULTILINE) is not None  # as run._regex searches

        for reply in ("runtimes claude ok, codex missing", "- `codex` is not installed", "Codex CLI isn't installed on this machine.",
                      "Codex CLI isn’t installed.", "codex: not found", "Missing runtime: codex", "**Not installed:** codex",
                      "| claude | ok |\n| codex | missing |", "codex (binary `codex`) is unavailable",
                      "The implement rotation names claude and codex; codex is missing.",
                      "`[models.implement]` lists `codex:gpt-x`, and codex is missing, so that entry cannot launch.",
                      "`[models.implement]` lists `codex:gpt-x`, which isn't available in the login shell.",
                      "`codex:gpt-x`, isn't available in the login shell", "Codex is not available on this machine."):
            self.assertTrue(hit(reply), reply)
        for reply in ("The implement rotation is claude:opus@high, then codex:gpt-x.", "codex is installed.", "Settings look fine.",
                      "claude is missing.", "runtimes codex ok, claude missing", "runtimes claude missing, codex ok",
                      "codex ok, cursor missing (agent)", "- codex: ok\n- claude: missing", "- claude: missing\n- codex: ok",
                      "Nothing is missing. Codex is there.", "The codex entry is second. One calendar is unavailable.",
                      "codex is not missing.", "Codex isn't missing.", "Not missing: codex"):
            self.assertFalse(hit(reply), reply)

    def test_missing_runtime_graders_read_the_health_call_and_the_named_runtime(self) -> None:
        """#48's acceptance case. The health grader is the bad-setting case's, which
        `test_bad_setting_graders_read_the_health_call_and_the_named_key` pins command by command."""
        s = spec(EVALS / "cases" / "open-day-names-a-missing-runtime")
        used, named = s["graders"]
        self.assertEqual(used, spec(EVALS / "cases" / "open-day-names-a-bad-setting")["graders"][0])
        self.assertEqual((used["type"], used["tool"], used["min"]), ("tool_used", "Bash", 1))
        self.assert_names_codex_as_missing(named)
        self.assertEqual(s["runtimes_present"], ["claude"])

    def test_sha_claim_graders_read_the_audit_call_and_the_row(self) -> None:
        """#49: a peer says its fix is on main at a sha, and the task's File ownership names no worktree. Each grader
        carries the one sentence it enforces. The call grader reads the `command` field for `audit --sha` with the sha's
        first seven hex (the call's description decides nothing); the row grader finds `done` in the state cell of the
        Upload path fix row at either table width and in no other row. There is no reply grader: the sentence the graders
        quote says to check first and close nothing, and says nothing about the reply's wording. Not told apart: an `echo`
        or `grep` whose text holds `chief-of-stuff audit --sha 9f3c1e7` (the convention anchors on the field, not on the
        position inside it)."""
        s = spec(EVALS / "cases" / "sha-claim-checked-through-audit")
        sha = re.search(r"\b[0-9a-f]{40}\b", s["prompt"]).group()
        rule = ("When that word names a sha as on main, check it first: `chief-of-stuff audit --sha <sha>` fetches and prints "
                "whether origin/main or local main holds it, and a sha it finds on neither closes nothing.")
        self.assertEqual(len(s["graders"]), 2)
        used, row = s["graders"]
        for g in (used, row):
            self.assertEqual(g["rule"], rule, g["name"])
        self.assertEqual((used["type"], used["tool"], used["min"]), ("tool_used", "Bash", 1))
        self.assertEqual((row["type"], row["path"], row["match"]), ("file_matches", "daily/{{today}}-tracker.md", "absent"))
        # The premise: Upload path fix names no worktree, so the old sentence would close it on its owner's word. The other
        # task owns one, so a git tree exists for `audit --sha` to ask (with none, it exits 2 and the coordinator cannot check).
        self.assertEqual(s["repo"]["worktrees"], [{"name": "wt-notes", "branch": "notes/draft", "merged": False}])
        self.assertNotIn("golden", s)
        case = EVALS / "cases" / "sha-claim-checked-through-audit"
        tracker = (case / "fixture" / "daily" / "{{today}}-tracker.md").read_text()
        owned = tracker.split("## File ownership")[1].split("## Log")[0]
        self.assertIn("- Upload path fix: src/a/\n", owned)
        self.assertIn("- Draft release notes: worktree `wt-notes`", owned)
        self.assertEqual([l for l in owned.splitlines() if "worktree" in l and "Upload" in l], [])
        # Built the way the harness builds it, the check runs and answers for a commit nobody has.
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            run.render_tree(case / "fixture", work, ctx(s))
            run.make_repo.build(work, run.render_value(s, ctx(s))["repo"])
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = audit_tasks.main(["--root", str(work), "--sha", sha])
            self.assertEqual((code, err.getvalue()), (1, ""))
            self.assertIn(f"sha {sha}: no such commit after fetch", out.getvalue())

        def called(command: str) -> bool:
            # The description is the model's prose about the call. It names the audit call here, and decides nothing.
            said = f"chief-of-stuff audit --sha {sha[:7]}"
            return re.search(used["input_match"], json.dumps({"command": command, "description": said})) is not None

        for command in (f"chief-of-stuff audit --sha {sha}", f"chief-of-stuff audit --sha {sha[:7]}", f"chief_of_stuff audit --sha {sha}",
                        f"chief-of-stuff audit --root . --sha {sha}", f"chief-of-stuff audit --sha={sha}",
                        f'chief-of-stuff audit --sha "{sha}"', f"chief-of-stuff audit --sha '{sha}'",
                        f"python3 /release/chief_of_stuff.py audit --sha {sha}",
                        f"python3 /release/scripts/audit_tasks.py --sha {sha}",
                        # The launcher is not part of the rule: a variable, a path, or `python3 x.py` before the subcommand word.
                        f"C=/some/path/results/20261003-121724/plugin/chief_of_stuff.py; python3 $C audit --sha {sha} 2>&1",
                        f'"$C" audit --sha={sha[:7]}',
                        f"python3 scripts/audit_tasks.py --root . --sha {sha}",
                        f"cd /tmp/ws; chief-of-stuff audit --date 2026-10-03 --sha {sha} 2>&1 | head -5",
                        # The field is anchored, the position inside it is not: an echo or a grep that holds the call text passes.
                        f'echo "chief-of-stuff audit --sha {sha[:7]}"', f"grep -n 'audit --sha {sha[:7]}' notes/plan.md; chief-of-stuff audit --sha {sha[:7]}"):
            self.assertTrue(called(command), command)
        for command in ("chief-of-stuff audit --date 2026-10-03", f"chief-of-stuff audit --sha 54b7eb3a5b2d4c6e8f0a1b3d5c7e9f2a4b6c8d0e",
                        f"chief-of-stuff audit --date {sha[:7]}", f"chief-of-stuff health --sha {sha}", f"chief-of-stuff auditor --sha {sha}",
                        f"git cat-file -t {sha}", f"git branch --contains {sha[:7]}", f"chief-of-stuff tracker tasks --not done",
                        f"chief-of-stuff audit --sha-file {sha[:7]}",
                        # `audit` must be a word of its own: not a longer word, a path segment, or a launcher's tail.
                        f"$C auditor --sha {sha}", f"$C audit --sha-file {sha[:7]}", f"$C health --sha {sha}",
                        f"$C audit --date 2026-10-03", f"$C audit --sha 54b7eb3a5b2d4c6e8f0a1b3d5c7e9f2a4b6c8d0e",
                        f"cat /audits/notes.md --sha {sha[:7]}", f"myaudit --sha {sha[:7]}", f"python3 myaudit_tasks.py --sha {sha[:7]}"):
            self.assertFalse(called(command), command)
        # Only the command field counts: another tool's input, or a description that names the call, does not.
        said = f"chief-of-stuff audit --sha {sha}"
        for tool_input in ({"file_path": "agents/chief-of-stuff.md"}, {"file_path": "agents/chief-of-stuff.md", "offset": 80, "limit": 20},
                           {"pattern": said, "path": "agents/chief-of-stuff.md"}, {"command": "ls", "description": said}):
            self.assertIsNone(re.search(used["input_match"], json.dumps(tool_input)), tool_input)

        def written(line: str) -> bool:
            return re.search(row["pattern"], line, re.MULTILINE) is not None  # as run._file_matches searches

        wide = "| Upload fix | Upload path fix | 4821-fix | {} | 09:00 |  | S |  |  |  | Checklist: Fix the upload path check |"
        for state in ("done", "done 09:30", "done 09:30–11:43", f"done 09:30–11:43 {sha[:7]}", f"done 09:30–11:43 {sha}",
                      "**done**", "Done", "`done 09:30–11:43`"):
            self.assertTrue(written(f"| Upload path fix | 4821-fix | {state} | 09:00 |  | Checklist: Fix the upload path check |"), state)
            self.assertTrue(written(wide.format(state)), state)
        # the name cell alone holds the task's words
        self.assertTrue(written("| Upload path fix | Fix the check | 4821-fix | done 09:30–11:43 | 09:00 |  | S |  |  |  | Checklist: Fix |"))
        for line in ("| Upload path fix | 4821-fix | running 09:30 | 09:00 |  | Checklist: Fix the upload path check |",
                     "| Upload path fix | 4821-fix | waiting | 09:00 |  | Checklist: Fix the upload path check |",
                     "| Upload path fix | 4821-fix | open | 09:00 |  | done once the sha is checked |",
                     wide.format("running 09:30"), wide.format("waiting"),
                     # another task's row does not count, whatever its state
                     "| Draft release notes | Robin | done 09:30–11:43 | 09:00 |  | Checklist: Draft release notes |",
                     "| Draft release notes | Draft the notes | Robin | done 09:30–11:43 | 09:00 |  | S |  |  |  | Checklist: Draft |",
                     f"- 11:43 4821-fix reports Upload path fix done at {sha[:7]}; waiting on the check"):
            self.assertFalse(written(line), line)

    def test_sha_claim_graders_enforce_a_sentence_the_agent_carries(self) -> None:
        """Split from the lint above so its rows run while the agent file still carries the old sentence."""
        rule = ("When that word names a sha as on main, check it first: `chief-of-stuff audit --sha <sha>` fetches and prints "
                "whether origin/main or local main holds it, and a sha it finds on neither closes nothing.")
        agent_text = (EVALS.parent / "agents" / "chief-of-stuff.md").read_text()
        graders = spec(EVALS / "cases" / "sha-claim-checked-through-audit")["graders"]
        self.assertEqual([g["type"] for g in graders], ["tool_used", "file_matches"])
        for g in graders:
            self.assertEqual(g["rule"], rule, g["name"])
            self.assertIn(g["rule"], agent_text, g["name"])

    def test_workflow_step_graders_enforce_a_sentence_the_agent_carries(self) -> None:
        """#362: every rule-bearing grader of the review case quotes the one sentence the agent file carries."""
        rule = ("A workflow step — a review, or anything the pipeline itself performs — takes no issue: "
                "write `workflow` in its `issue` cell.")
        agent_text = (EVALS.parent / "agents" / "chief-of-stuff.md").read_text()
        graders = spec(EVALS / "cases" / "review-step-takes-the-workflow-token")["graders"]
        self.assertEqual([g["type"] for g in graders], ["file_matches", "tool_used", "file_matches", "timestamp_tolerance"])
        for g in graders[:3]:
            self.assertEqual(g["rule"], rule, g["name"])
            self.assertIn(g["rule"], agent_text, g["name"])

    def test_workflow_step_graders_catch_what_they_enforce(self) -> None:
        """#366 review R3/R4: the graders of the review case are applied to sample commands and tracker rows. Positives are
        commands that file an issue and rows that are right; negatives are reads and rows that are wrong."""
        rule = ("A workflow step — a review, or anything the pipeline itself performs — takes no issue: "
                "write `workflow` in its `issue` cell.")
        row_g, filed_g, number_g = spec(EVALS / "cases" / "review-step-takes-the-workflow-token")["graders"][:3]
        for g in (row_g, filed_g, number_g):
            self.assertEqual(g["rule"], rule, g["name"])
        said = "chief-of-stuff backlog --list"

        def filed(command: str) -> bool:
            # The description is the model's prose about the call; only the command field counts.
            return re.search(filed_g["input_match"], json.dumps({"command": command, "description": said})) is not None

        for command in ('chief-of-stuff backlog --create --title "Review MR 14"', "backlog --create --title x",
                        "chief-of-stuff backlog --config CLAUDE.md --create --title x",
                        'python3 scripts/backlog.py --body "x" --create --title y',
                        'backlog.py --body "x" --create', "gh issue create --title x", "gh issue  create --title x",
                        "gh -R o/backlog issue create --title x", "gh api repos/o/backlog/issues",
                        "gh api repos/o/backlog/issues -f title=x", "glab issue create --title x",
                        "cd ws && glab issue create -t x", 'echo "$C" > /dev/null; chief-of-stuff backlog --create --title x'):
            self.assertTrue(filed(command), command)
        for command in ("chief-of-stuff backlog --list", "backlog --config CLAUDE.md --list", "gh issue view 3", "gh issue list",
                        "glab issue view 3", "gh api repos/o/backlog/issues/3", "gh pr view 14", "gh pr create --title x",
                        "git log --oneline"):
            self.assertFalse(filed(command), command)
        # The description names the call and decides nothing.
        self.assertIsNone(re.search(filed_g["input_match"], json.dumps(
            {"command": "ls", "description": "chief-of-stuff backlog --create; gh issue create"})))

        def row(name: str, issue: str, item: str = "Look over the change") -> str:
            return f"| {name} | {item} | unassigned | open | 09:00 |  | S | build | pr | {issue} | Checklist: look |"

        def tracker(line: str) -> str:
            return f"## Tasks\n\n| name | item | owner |\n|---|---|---|\n| Cut the release | Cut it | robin | open |\n{line}\n\n## Log\n"

        def has(g: dict, line: str) -> bool:
            return re.search(g["pattern"], tracker(line), re.MULTILINE) is not None

        for line in (row("Review MR 14", "workflow"), row("Reviewing MR 14", "workflow"), row("Merge request 14", "workflow"),
                     row("MR check", "workflow", "Go over merge request 14 on o/app"), row("Review of the change", "workflow"),
                     row("Review MR 14", " workflow  ")):
            self.assertTrue(has(row_g, line), line)
        for line in (row("Review MR 14", "Workflow"), row("Review MR 14", "WORKFLOW"), row("Review MR 14", "workflow."),
                     row("Review MR 14", "workflows"), row("Review MR 14", "my workflow"), row("Review MR 14", "#14"),
                     row("Review MR 14", ""), row("Cut the release notes", "workflow")):
            self.assertFalse(has(row_g, line), line)
        self.assertEqual(number_g.get("match"), "absent")
        for issue in ("#14", "#7", "o/backlog#14", "o/app#9", "https://github.com/o/backlog/issues/14"):
            for name in ("Review MR 14", "Reviewing MR 14", "Merge request 14"):
                self.assertTrue(has(number_g, row(name, issue)), (name, issue))
        for line in (row("Review MR 14", "workflow"), row("Reviewing MR 14", "workflow"), row("Cut the release", "#9"),
                     row("Review MR 14", "")):
            self.assertFalse(has(number_g, line), line)

    def test_effort_graders_enforce_a_sentence_the_agent_carries(self) -> None:
        """#52: every grader of the effort case quotes the one sentence the agent file carries."""
        rule = ("Launch an entry as `--runtime <runtime> --model <model-id>`, plus `--effort <effort>` when the entry has one, "
                "with the model ID verbatim.")
        agent_text = (EVALS.parent / "agents" / "chief-of-stuff.md").read_text()
        for g in spec(EVALS / "cases" / "models-effort-whatever-the-runtime")["graders"][:3]:
            self.assertEqual(g["rule"], rule, g["name"])
            self.assertIn(g["rule"], agent_text, g["name"])

    def test_effort_graders_judge_each_worker_launch_for_the_codex_entry(self) -> None:
        """#52: the suggested entry is `codex:gpt-x@medium`. Per launch (a `worker` call with `--task`, up to the next `;`, `&`,
        `|` or line break, a quoted argument stepped over whole), the call carries `--runtime codex --model gpt-x --effort medium`
        or `--class implement` and none of the three flags. Three graders read the same call, as `run._tool_used` does (the
        regex searches the call's JSON): the first is the satisfying launch (min 1), the second any launch that omits the
        effort or runs another (max 0), the third any launch of another runtime or model (max 0). A Claude launch
        (`--runtime claude --model sonnet --effort medium`) carries the effort, so the second lets it through; it does not
        satisfy the first, and the third is what fails it, the way `models-rotation-first-entry` splits "launches the first
        entry" from "launches no other model". Ceiling: a quote left open, or a quote with a `\\"` inside it, ends the
        command early, and the flags after it go unseen."""
        ok, no_effort, other = (g["input_match"] for g in spec(EVALS / "cases" / "models-effort-whatever-the-runtime")["graders"][:3])
        w = 'chief-of-stuff worker --root . --task "T"'
        tail = "--one-shot --dry-run"

        def hits(pattern: str, command: str) -> bool:
            # The description is the model's prose about the call. It names flags here, and decides nothing.
            said = "Dry-run the worker with --runtime claude --model sonnet --effort medium --class implement"
            return re.search(pattern, json.dumps({"command": command, "description": said})) is not None

        # command: (satisfies "the task is dispatched", is a "launch that omits the effort", is a "launch of another entry")
        table = {
            f"{w} --runtime codex --model gpt-x --effort medium {tail}": (True, False, False),
            f"{w} --class implement {tail}": (True, False, False),
            f'{w} --runtime "codex" --model \'gpt-x\' --effort "medium" {tail}': (True, False, False),
            f"{w} --class 'implement' {tail}": (True, False, False),
            f'{w} --runtime codex --model gpt-x --effort=medium {tail}': (True, False, False),
            f"python3 /release/chief_of_stuff.py worker --effort medium --runtime codex --model gpt-x --task x {tail}": (True, False, False),
            # a second command that is not a launch: its `--model` is not this launch's
            f"{w} --class implement --dry-run; chief-of-stuff worker --help | grep -- --model": (True, False, False),
            f"chief-of-stuff worker --help | grep -- --model; {w} --class implement --dry-run": (True, False, False),
            f"{w} --class implement --dry-run\nchief-of-stuff worker --help | grep -- --model": (True, False, False),
            f"cd /ws && {w} --class implement --dry-run && echo '--model x'": (True, False, False),
            # the launcher held in a variable, as a stored run had it
            "P=/r/plugin/chief_of_stuff.py; python3 $P worker --root . --one-shot --runtime codex --model gpt-x --effort medium "
            '--name n --task "T" --dry-run 2>&1': (True, False, False),
            'python3 "$P" worker --task "T" --runtime codex --model gpt-x --dry-run': (False, True, False),
            'python3 $P worker --task "T" --class implement --dry-run': (True, False, False),
            # a `;` inside the task text does not end the command
            'chief-of-stuff worker --task "Fix the export; add tests" --class implement --dry-run': (True, False, False),
            # no effort
            f"{w} --runtime codex --model gpt-x --dry-run": (False, True, False),
            f"{w} --runtime codex --model gpt-x --dry-run; echo '--effort medium'": (False, True, False),
            f"{w} --runtime codex --model gpt-x --dry-run\necho '--effort medium'": (False, True, False),
            f"{w} --runtime codex --model gpt-x --dry-run && chief-of-stuff worker --help --effort medium": (False, True, False),
            # explicit flags disable the class, so no effort
            f"{w} --class implement --runtime codex --model gpt-x --dry-run": (False, True, False),
            f"{w} --class implement --model gpt-x --dry-run": (False, True, False),
            # another effort than the entry's
            f"{w} --class implement --effort low --dry-run": (False, True, False),
            f"{w} --runtime codex --model gpt-x --effort high --dry-run": (False, True, False),
            f"{w} --runtime codex --model gpt-x --effort mediumish --dry-run": (False, True, False),
            # the other entry, with an effort: not the suggested one
            f"{w} --runtime claude --model sonnet --effort medium --dry-run": (False, False, True),
            f"{w} --runtime codex --model gpt-xl --effort medium --dry-run": (False, False, True),
            f"{w} --runtime codex --model gpt-x --effort medium --dry-run; {w} --runtime claude --model sonnet --effort medium": (True, False, True),
            f"{w} --runtime codex --model gpt-x --effort medium --dry-run; {w} --class implement --effort low --dry-run": (True, True, False),
            # not a launch
            "chief-of-stuff worker --help | grep -- --model": (False, False, False),
            "chief-of-stuff models --root . --class implement": (False, False, False),
            f"{w} --dry-run": (False, True, False),
        }
        for command, want in table.items():
            self.assertEqual(tuple(hits(p, command) for p in (ok, no_effort, other)), want, command)

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

    def test_worktree_clone_name_graders_enforce_a_sentence_the_agent_carries(self) -> None:
        """#54: the settings name two repositories and the ready task is one project's, so the worktree call must carry that project's
        name as `--clone <repo>`. Two graders read the call (the name is passed; no other `--clone` value is) and one reads the tree it cut."""
        agent_text = (EVALS.parent / "agents" / "chief-of-stuff.md").read_text()
        case = EVALS / "cases" / "worktree-clone-is-a-repos-name"
        s = spec(case)
        self.assertNotIn("golden", s)
        named, other, belongs = s["graders"]
        for g in (named, other, belongs):
            self.assertEqual(g["rule"], REPOS_RULE, g["name"])
            self.assertIn(g["rule"], agent_text, g["name"])
        self.assertEqual([(g["type"], g.get("min"), g.get("max")) for g in (named, other)], [("tool_used", 1, None), ("tool_used", None, 0)])
        # The tree grader reads the harness's one repository; it cannot tell a name from a path, so the call graders do.
        self.assertEqual((belongs["type"], belongs["glob"], belongs["pattern"]), ("file_matches", "trees/*/.git", r"^gitdir: .*/birch-checkout/\.git/worktrees/"))
        self.assertNotIn("match", belongs, "an absent match inverts the grader")
        command = next(l for l in agent_text.splitlines() if l.startswith("chief-of-stuff worktree"))
        self.assertEqual(command, "chief-of-stuff worktree --type <type> --name <tree> --branch <branch> --root . --clone <repo>")
        fixture = case / "fixture"
        repos = load_settings(fixture, settings_path((fixture / "CLAUDE.md").read_text())).repos
        self.assertEqual(repos, {"alder": "alder-checkout", "birch": "birch-checkout"})
        self.assertEqual(s["repo"]["clone"], repos["birch"], "the repository the harness builds is the task's")
        self.assertTrue((fixture / repos["alder"]).is_dir(), "the other entry names a directory, not nothing")
        self.assertFalse((fixture / repos["alder"] / ".git").exists())
        # The task is plainly the birch project's, in the places a coordinator reads.
        tracker = next((fixture / "daily").glob("*-tracker.md")).read_text()
        self.assertEqual(s["prompt"], "Start the birch security audit. For this isolated test, use --dry-run for the worker launch and do not start a real worker process.")
        self.assertRegex(tracker, r"(?m)^\| Birch security audit \|")
        self.assertRegex(tracker, r"(?m)^- Birch security audit: .*birch")
        self.assertNotIn("alder", tracker.lower())

        worktree = "chief-of-stuff worktree --type implementer --name audit --branch feat/audit --root ."
        for command, passes, wrong in (
            (worktree, False, False),
            (f"{worktree} --clone birch", True, False),
            (f"{worktree} --clone  birch", True, False),
            (f"{worktree} --clone=birch", True, False),
            (f'{worktree} --clone "birch"', True, False),
            (f"{worktree} --clone 'birch'", True, False),
            (f"{worktree} --clone birch && chief-of-stuff worker --type implementer --cwd trees/audit", True, False),
            (f'python3 "$PLUGIN/scripts/make_worktree.py" --type implementer --name a --branch b --root . --clone birch', True, False),
            (f"chief-of-stuff worktree --type implementer --name audit-checkout --branch feat/audit --root . --clone birch", True, False),
            (f"chief-of-stuff worktree --type implementer --name audit --branch audit/alder-fix --root . --clone birch", True, False),
            (f"{worktree} --clone birch --from-local && ls alder-checkout", True, False),
            (f"{worktree} --clone birch-checkout", False, True),
            (f"{worktree} --clone ./birch-checkout", False, True),
            (f"{worktree} --clone /abs/birch-checkout", False, True),
            (f"{worktree} --clone birch/", False, True),
            (f"{worktree} --clone birchwood", False, True),
            (f"{worktree} --clone alder", False, True),
            (f"{worktree} --clone=alder", False, True),
            (f'{worktree} --clone "alder"', False, True),
            (f"{worktree} --clone alder-checkout", False, True),
            (f'{worktree} --clone ""', False, True),
            (f"{worktree} --clone alder && chief-of-stuff worktree --type implementer --name b --branch c --root . --clone birch", True, True),
            (f"{worktree} && chief-of-stuff worker --type implementer --cwd trees/audit --clone birch", False, False),
            ("chief-of-stuff worker --type implementer --cwd alder", False, False),
        ):
            with self.subTest(command=command):
                self.assertEqual(grader_hits(named, "Bash", command=command), passes)
                self.assertEqual(grader_hits(other, "Bash", command=command), wrong)
        # The gitdir names the task's repository, not the other entry's and not a sibling that shares its prefix.
        for gitdir, owned in (("gitdir: /ws/birch-checkout/.git/worktrees/audit", True), ("gitdir: /ws/alder-checkout/.git/worktrees/audit", False),
                              ("gitdir: /ws/birch-checkout-old/.git/worktrees/audit", False)):
            with self.subTest(gitdir=gitdir):
                self.assertEqual(re.search(belongs["pattern"], gitdir, re.MULTILINE) is not None, owned)

    def test_disjoint_one_shot_graders_quote_the_agent_file(self) -> None:
        """#365 review R9: every grader that judges a launch quotes a sentence the agent file carries. The launch sentence is the
        background one (its exact text is in test_dispatch_prompt's one-shot lint)."""
        agent_text = (EVALS.parent / "agents" / "chief-of-stuff.md").read_text()
        report, export, single, limits, handler, rows = spec(EVALS / "cases" / "one-shot-disjoint-tasks-launch-together")["graders"][:6]
        for g in (report, export, single, limits, handler, rows):
            self.assertIn(g["rule"], agent_text, g["name"])
        # The graders that count launches quote the BACKGROUND sentence (one call each, background, no chain, no pipe); the one that
        # holds back an overlapping task quotes LAUNCH. Both are pinned verbatim in test_dispatch_prompt's one-shot lint.
        self.assertEqual({g["rule"] for g in (report, export, single)}, {report["rule"]})
        self.assertEqual(report["rule"], "Give each launch its own Bash call with `run_in_background: true`; never chain launches with `;` or `&&`, "
                                          "never pipe a launch, and never start one without the flag. "
                                          "Add no `| head`, `| tail` or `| cut` to a launch: its whole output is read from the notification's output file.")
        self.assertIn("overlap no running task", limits["rule"])
        self.assertIn("launch the earlier row first and the other when it returns", limits["rule"])

    def test_disjoint_one_shot_launch_graders_match_a_background_launch_and_nothing_else(self) -> None:
        """#365 review R1-R3: what the graders count. `worker --check` and a `--dry-run` after `;` are not launches, a launch is one
        simple command run in the background (the harness records `run_in_background` in the call's input), and a running or
        overlapping task is graded by any launch of it, foreground or not. Cannot see: a loop over a shell variable."""
        report, export, single, limits, handler, rows = spec(EVALS / "cases" / "one-shot-disjoint-tasks-launch-together")["graders"][:6]
        self.assertEqual([(g["type"], g["tool"], g.get("min"), g.get("max")) for g in (report, export, single, limits, handler)],
                         [("tool_used", "Bash", 1, None)] * 2 + [("tool_used", "Bash", None, 0)] * 3)
        said = "chief-of-stuff worker --task 'Fix report writer' --dry-run"  # the model's prose about a call decides nothing

        def hit(g: dict, command: str, background: bool = True) -> bool:
            return grader_hits(g, "Bash", command=command, description=said, run_in_background=background)

        def first(g: dict, command: str) -> bool:  # `run_in_background` ahead of the command, as a model may order the keys
            return bool(re.search(g["input_match"], json.dumps({"run_in_background": True, "command": command})))

        for g, name in ((report, "Fix report writer"), (export, "Fix export header")):
            for command in (f"chief-of-stuff worker --root . --task '{name}' --dry-run", f'chief-of-stuff worker --task "{name}" --dry-run',
                            f"python3 /r/chief_of_stuff.py worker --dry-run --task={name}",
                            f"python3 /r/scripts/spawn_session.py --task '{name}' --dry-run",
                            f"chief-of-stuff worker --task '{name}' --dry-run 2>&1",  # a redirect is no pipe
                            f"chief-of-stuff worker --task '{name}' --dry-run > /dev/null 2>&1",
                            f"cd /ws && chief-of-stuff worker --root . --task '{name}' --dry-run --one-shot\n"):
                self.assertTrue(hit(g, command), command)
            self.assertTrue(first(g, f"chief-of-stuff worker --task '{name}' --dry-run"))
            for label, command, background in (
                    ("a foreground launch", f"chief-of-stuff worker --task '{name}' --dry-run", False),
                    ("a check", f"chief-of-stuff worker --check --root . --task '{name}' --one-shot", True),
                    ("a check with a dry run", f"chief-of-stuff worker --check --task '{name}' --one-shot --dry-run", True),
                    ("a check after the task", f"chief-of-stuff worker --task '{name}' --check --dry-run", True),
                    ("a dry run after a semicolon", f"chief-of-stuff worker --task '{name}'; echo --dry-run", True),
                    ("a piped launch", f"chief-of-stuff worker --one-shot --dry-run --task '{name}' 2>&1 | tail -8", True),
                    ("a launch piped with stderr", f"chief-of-stuff worker --task '{name}' --dry-run |& tail -3", True),
                    ("a launch piped to tee", f"chief-of-stuff worker --dry-run --task '{name}' | tee log.txt", True),                    ("a dry run on the next line", f"chief-of-stuff worker --task '{name}'\nchief-of-stuff worker --task 'Other' --dry-run", True),
                    ("another task", "chief-of-stuff worker --task 'Fix upload handler' --dry-run", True),
                    ("a mention", f"echo chief-of-stuff worker; cat tracker.md # --task '{name}' --dry-run", True),
                    ("a worktree call", "chief-of-stuff worktree --name fix-it --branch b", True)):
                self.assertFalse(hit(g, command, background), f"{label} for {name}")
        # The running task and the one that overlaps it: any launch counts, a check does not (R1).
        for g, name in ((limits, "Add upload limits"), (handler, "Fix upload handler")):
            for command, background in ((f"chief-of-stuff worker --task '{name}' --dry-run", True), (f"chief-of-stuff worker --task '{name}' --dry-run", False),
                                        (f"chief-of-stuff worker --task '{name}'", True), (f'chief-of-stuff worker --one-shot --task "{name}" --cwd t', False)):
                self.assertTrue(hit(g, command, background), command)
            for command in (f"chief-of-stuff worker --check --root . --task '{name}' --one-shot", f"chief-of-stuff worker --task '{name}' --one-shot --check",
                            f"chief-of-stuff worker --check --task '{name}' --one-shot --dry-run"):
                self.assertFalse(hit(g, command), command)
        # One call launches one task: two `--task` arguments in one command that is not a check.
        self.assertTrue(hit(single, "chief-of-stuff worker --task 'Fix report writer' --dry-run; chief-of-stuff worker --task 'Fix export header' --dry-run"))
        self.assertFalse(hit(single, "chief-of-stuff worker --task 'Fix report writer' --dry-run"))
        self.assertFalse(hit(single, "chief-of-stuff worker --check --task 'Fix report writer' --one-shot; chief-of-stuff worker --check --task 'Fix export header' --one-shot"))
        self.assertFalse(hit(single, "chief-of-stuff worker --task 'Fix report writer' --dry-run; chief-of-stuff log --root . 'launched Fix report writer and Fix export header'"))

    def test_one_shot_in_flight_graders_enforce_sentences_the_agent_carries(self) -> None:
        """#53: a running one-shot is neither launched again (the readiness sentence) nor looked up with `ps`/`pgrep`, and is looked up with
        `processes`: both of those are the helper sentence, which sends liveness to the helper instead of `ps`/`pgrep`. Every grader quotes a
        sentence the agent file carries. No agent sentence says `processes` also answers for one-shot runs; what it lists is
        pinned by test_process_status.py, and a reply that says the run stopped is not graded."""
        agent_text = (EVALS.parent / "agents" / "chief-of-stuff.md").read_text()
        launch, asks, listing = spec(EVALS / "cases" / "one-shot-in-flight-is-checked-with-processes")["graders"]
        self.assertIn("Ready means an open, unassigned task", launch["rule"])
        helper = "Use this helper instead of ad hoc `ps` or `pgrep` calls, and do not mark an `unverified` worker gone."
        self.assertEqual((asks["rule"], listing["rule"]), (helper, helper))
        for g in (launch, asks, listing):
            self.assertIn(g["rule"], agent_text, g["name"])
        self.assertEqual([(g["type"], g["tool"], g["max"]) for g in (launch, listing)], [("tool_used", "Bash", 0)] * 2)
        self.assertEqual((asks["type"], asks["tool"], asks["min"], "max" in asks), ("tool_used", "Bash", 1, False))

        def hit(g: dict, command: str) -> bool:
            # The description is the model's prose about the call. It names the task and `ps`, and decides nothing.
            said = "chief-of-stuff worker --task 'Rate limit headers'; ps; chief-of-stuff processes"
            return re.search(g["input_match"], json.dumps({"command": command, "description": said})) is not None

        # `--task` names the running task, in the forms a launcher call takes. A variable is followed when the same command
        # assigns the task text to it. Cannot see: a task text built from pieces, or read from a file.
        for command in ("chief-of-stuff worker --task 'Rate limit headers' --dry-run", 'chief-of-stuff worker --task "Rate limit headers"',
                        "python3 /release/chief_of_stuff.py worker --runtime codex --task 'Rate limit headers' --dry-run",
                        "python3 /release/scripts/spawn_session.py --task=Rate limit headers",
                        "chief-of-stuff worker --task $'Rate limit headers'", "chief-of-stuff worker --task=$'Rate limit headers' --dry-run",
                        "T='Rate limit headers'; chief-of-stuff worker --task \"$T\"", 'T="Rate limit headers"\nchief-of-stuff worker --task "${T}"',
                        "P=/r/plugin/chief_of_stuff.py; python3 $P worker --task 'Rate limit headers' --dry-run",
                        'python3 "$P" worker --task "Rate limit headers"'):
            self.assertTrue(hit(launch, command), command)
        for command in ("chief-of-stuff worker --task 'Draft release notes' --dry-run", "chief-of-stuff processes --root .",
                        "echo chief-of-stuff worker --task Other", "cat daily/tracker.md # Rate limit headers",
                        "T='Draft release notes'; chief-of-stuff worker --task \"$T\"", "T='Rate limit headers'; echo \"$T\"",
                        "T='Rate limit headers'; chief-of-stuff worker --task \"$U\""):
            self.assertFalse(hit(launch, command), command)

        # `ps`, `pgrep` and `pkill` count at command position: the start of the command, or after `;`, `&`, `|`, `(`, `$(`,
        # a backtick, or a line break (the harness records one as the two characters `\n`), a path in front allowed.
        # Cannot see: a quoted `;` or `|` (`echo 'a; ps'`), a prefix word (`sudo ps`, `xargs pgrep`), a command held in a variable.
        for command in ("pgrep -fl trees/rate-limit", "ps aux | grep rate-limit", "ps -p 1", "cd trees && ps -ef", "pgrep -f x | wc -l",
                        "echo $(ps -o pid= -p 1)", "/bin/ps -ef", "pkill -0 -f trees/rate-limit", "ls trees; pgrep codex",
                        "ps", "ps;ls", "ps|grep codex", "ps\t-ef", "pgrep", "ls\nps\n", "ls\n\tps -ef", "ls &&ps", "(ps -ef)", "`ps`",
                        "echo x; \tps", "pgrep -f x\n"):
            self.assertTrue(hit(listing, command), command)
        for command in ("chief-of-stuff processes --root .", "python3 /release/scripts/process_status.py --root .",
                        "chief-of-stuff worker --task 'Rate limit headers' --dry-run", "grep -rn steps notes/", "cat trees/rate-limit/.chief-of-stuff/one-shot.pid",
                        "cat daily/tracker.md; ls maps", "chief-of-stuff tracker tasks --not done",
                        "chief-of-stuff processes --root . # not ps or pgrep", "chief-of-stuff processes --root . && echo no ps needed",
                        "grep -n 'ps ' agents/x.md", "docker ps -a", "git log --grep 'ps aux'", "echo ps", "ls pspdf", "psql -c x", "python3 ps.py",
                        "cat tps.log", "ls\ntpsx\n"):
            self.assertFalse(hit(listing, command), command)

        # `processes` is run: the entry point by name, by script path, or by a launcher variable, at command position.
        for command in ("chief-of-stuff processes --root .", "chief-of-stuff processes --root . 1", "python3 /release/chief_of_stuff.py processes --root .",
                        "cd /ws && chief-of-stuff processes --root .", "P=/r/plugin/chief_of_stuff.py; python3 $P processes --root .",
                        'python3 "$P" processes --root .', "$P processes", "python3 /release/scripts/process_status.py --root .",
                        "process_status.py", "ls\nchief-of-stuff processes --root .", "/usr/local/bin/chief-of-stuff processes"):
            self.assertTrue(hit(asks, command), command)
        for command in ("ps -p 1", "cat trees/rate-limit/.chief-of-stuff/one-shot.pid", "chief-of-stuff worker --task 'x' --dry-run",
                        "chief-of-stuff tracker tasks --not done", "echo chief-of-stuff processes", "grep -n processes agents/x.md",
                        "cat notes/chief-of-stuff processes.md", "chief-of-stuff processes-old", "ls # chief-of-stuff processes"):
            self.assertFalse(hit(asks, command), command)

    def test_one_shot_in_flight_fixture_is_a_run_the_launcher_wrote_and_processes_lists(self) -> None:
        """#53: the case is only worth running when `processes` really prints the one-shot. Rendered the way the harness
        renders it, the harness's own pid (`{{live_pid}}`, alive for the whole run) and the pid file's task come back as the one-shot row, and the tracker's
        started line has the launcher's own shape."""
        import process_status
        import tracker_log
        case = EVALS / "cases" / "one-shot-in-flight-is-checked-with-processes"
        s = spec(case)
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            run.render_tree(case / "fixture", work, ctx(s))
            run.make_repo.build(work, run.render_value(s, ctx(s))["repo"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(process_status.main(["--root", str(work)]), 0)
            from _vendor.toon_format import decode
            printed = decode(out.getvalue())
            self.assertIsInstance(printed, list, out.getvalue())  # one list: no registered worker here, so the one-shot row alone
            self.assertEqual(printed, [{"pid": os.getpid(), "task": "Rate limit headers", "worktree": "rate-limit",
                                        "status": "running", "kind": "one-shot"}])
            tracker = next(work.glob("daily/*-tracker.md")).read_text()
            started = tracker_log.started_line("00:00", "rate-limit", "codex", "gpt-5.1-codex", "trees/rate-limit", "Rate limit headers")
            self.assertIn(started.split(" ", 2)[2], tracker)
            self.assertRegex(tracker, r"(?m)^\| Rate limit headers \| rate-limit \| running \d\d:\d\d \|")


    def test_one_shot_report_graders_enforce_a_sentence_the_agent_carries(self) -> None:
        """#56: the three graders that judge how a finished one-shot's report is read quote one sentence, and the agent file
        must carry it. There is no fourth: no rule sentence states what the reply says, so a reply grader would assert the
        author's expected answer. The sentence's "or a host output file" half is asserted by no grader."""
        agent_text = (EVALS.parent / "agents" / "chief-of-stuff.md").read_text()
        called, shell, files = spec(EVALS / "cases" / "one-shot-report-is-read-with-result")["graders"]
        rule = ("Read a finished one-shot's report with `chief-of-stuff result --root . --task <task>`, "
                "never by reading files under its worktree or a host output file.")
        for g in (called, shell, files):
            self.assertEqual(g["rule"], rule, g["name"])
            self.assertIn(g["rule"], agent_text, g["name"])
        self.assertEqual((called["type"], called["tool"], called["min"], "max" in called), ("tool_used", "Bash", 1, False))
        self.assertEqual([(g["type"], g["tool"], g["max"]) for g in (shell, files)],
                         [("tool_used", "Bash", 0), ("tool_used", "Read|Grep", 0)])

        def hit(g: dict, command: str) -> bool:
            # The description is the model's prose about the call. It names the task and the files, and decides nothing.
            said = "chief-of-stuff result --task 'Rate limit headers'; cat one-shot-report.toon"
            return re.search(g["input_match"], json.dumps({"command": command, "description": said})) is not None

        # `result` is run, at command position, by name, script path or launcher variable, with `--task` naming the task.
        # Cannot see: a task text held in a shell variable, or built from pieces.
        for command in ("chief-of-stuff result --root . --task 'Rate limit headers'", 'chief-of-stuff result --task "Rate limit headers" --root .',
                        "chief-of-stuff result --root . --task=Rate limit headers", "cd /ws && chief-of-stuff result --root . --task 'Rate limit headers'",
                        "python3 /release/chief_of_stuff.py result --root . --task 'Rate limit headers'",
                        "python3 /release/scripts/one_shot_result.py --root . --task 'Rate limit headers'",
                        "P=/r/plugin/chief_of_stuff.py; python3 $P result --root . --task 'Rate limit headers'",
                        'python3 "$P" result --root . --task "Rate limit headers"', "ls\nchief-of-stuff result --root . --task $'Rate limit headers'",
                        "/usr/local/bin/chief-of-stuff result --root . --task 'Rate limit headers' | head",
                        "chief-of-stuff result --task 'Rate limit headers' --root . && echo done",
                        # the plugin form the agent file prescribes (agents/chief-of-stuff.md), quoted paths, and the TZ prefix run.py allows
                        'python3 ${CLAUDE_PLUGIN_ROOT}/chief_of_stuff.py result --root . --task "Rate limit headers"',
                        "python3 \"$CLAUDE_PLUGIN_ROOT/chief_of_stuff.py\" result --root . --task 'Rate limit headers'",
                        'python3 "/opt/plugin/chief_of_stuff.py" result --root . --task "Rate limit headers"',
                        "TZ=America/Chicago chief-of-stuff result --root . --task 'Rate limit headers'",
                        'TZ=America/Chicago python3 ${CLAUDE_PLUGIN_ROOT}/chief_of_stuff.py result --root . --task "Rate limit headers"',
                        "cd \"/my ws\" && chief-of-stuff result --root . --task 'Rate limit headers'",
                        "out=$(chief-of-stuff result --root . --task 'Rate limit headers')",
                        'chief-of-stuff result --root . --task="Rate limit headers"'):
            self.assertTrue(hit(called, command), command)
        # `--root` is part of the quoted call: without it the command exits 2, and an attempt counts.
        for command in ("chief-of-stuff result --root .", "chief-of-stuff result --root . --task 'Draft release notes'",
                        "chief-of-stuff result --task 'Rate limit headers'", "python3 /release/chief_of_stuff.py result --task 'Rate limit headers'",
                        "chief-of-stuff processes --root . --task 'Rate limit headers'", "echo chief-of-stuff result --task 'Rate limit headers'",
                        "chief-of-stuff results --task 'Rate limit headers'", "chief-of-stuff worker --task 'Rate limit headers' --dry-run",
                        # `--root` in a later command of the line is not `result`'s own: it exits 2 all the same
                        "chief-of-stuff result --task 'Rate limit headers' && chief-of-stuff processes --root .",
                        "chief-of-stuff result --task 'Rate limit headers'; chief-of-stuff processes --root .",
                        "chief-of-stuff result --task 'Rate limit headers' | chief-of-stuff processes --root .",
                        "chief-of-stuff result --task 'Rate limit headers' # --root .",
                        "chief-of-stuff result --task 'Rate limit headers'\nchief-of-stuff processes --root .",
                        "cat trees/rate-limit/.chief-of-stuff/one-shot-report.toon", "chief-of-stuff tracker tasks # result --task 'Rate limit headers'",
                        # nor is a `--task` in a later command, for the same reason `--root` is not
                        "chief-of-stuff result --root . ; chief-of-stuff processes --task 'Rate limit headers'",
                        "chief-of-stuff result --root . && chief-of-stuff processes --task 'Rate limit headers'",
                        "chief-of-stuff result --root . | chief-of-stuff processes --task 'Rate limit headers'",
                        "chief-of-stuff result --root . & chief-of-stuff processes --task 'Rate limit headers'",
                        "chief-of-stuff result --root . # --task 'Rate limit headers'",
                        "chief-of-stuff result --root .\nchief-of-stuff processes --task 'Rate limit headers'",
                        # Only a command's start counts: `result` after `echo`, in a comment, in a quote or in a heredoc is text, not a run
                        "echo chief-of-stuff result --root . --task 'Rate limit headers'",
                        "echo python3 ${CLAUDE_PLUGIN_ROOT}/chief_of_stuff.py result --root . --task 'Rate limit headers'",
                        "true # ; chief-of-stuff result --root . --task 'Rate limit headers'",
                        "# chief-of-stuff result --root . --task 'Rate limit headers'",
                        "echo \"run: | chief-of-stuff result --root . --task 'Rate limit headers'\"",
                        "echo 'run; chief-of-stuff result --root . --task \"Rate limit headers\"'",
                        "cat <<'EOF'\nchief-of-stuff result --root . --task 'Rate limit headers'\nEOF",
                        "cat > notes.txt <<EOF\nchief-of-stuff result --root . --task 'Rate limit headers'\nEOF",
                        # the whole task is named, not a prefix of a longer one
                        "chief-of-stuff result --root . --task 'Rate limit headers XYZ'", 'chief-of-stuff result --root . --task "Rate limit headers 2"',
                        'python3 ${CLAUDE_PLUGIN_ROOT}/chief_of_stuff.py result --root . --task "Rate limit headers (old)"'):
            self.assertFalse(hit(called, command), command)

        # The report, the worker's stdout log, anything in a tree's private directory, and the launcher's own copies (`result` reads those), by name,
        # glob or `.toon` suffix. Cannot see: a path built from variables, or a read after a `cd` that names no private path.
        for command in ("cat trees/rate-limit/.chief-of-stuff/one-shot-report.toon", "head -50 trees/rate-limit/.chief-of-stuff/worker-stdout.log",
                        "tail -n 20 /w/trees/rate-limit/.chief-of-stuff/worker-stdout.log", "python3 -c 'print(open(\"one-shot-report.toon\").read())'",
                        "grep reason trees/rate-limit/.chief-of-stuff/one-shot-report.toon", "cd trees/rate-limit && sed -n 1,5p .chief-of-stuff/one-shot-report.toon",
                        "cat .chief-of-stuff/reports/rate-limit.toon", "ls .chief-of-stuff/reports/", "head -50 /w/.chief-of-stuff/reports/rate-limit.toon",
                        "cd .chief-of-stuff/reports && cat *", "less trees/rate-limit/.chief-of-stuff/findings.md", "ls trees/rate-limit/.chief-of-stuff/", "awk 1 /w/trees/x/.chief-of-stuff/draft.md",
                        # a bare file name (after a `cd`, or from a find): only the file-name alternatives see these
                        "cat one-shot-report.toon", "cat worker-stdout.log",
                        # a glob names the same files: in the tree's private directory, in the launcher's reports, or by their suffix
                        "cat trees/rate-limit/.chief*/one-shot*", "cat trees/*/.chief*/*", "head .chief-of-stuff/rep*/*",
                        "cat .chief-of-stuff//reports/rate-limit.toon", "cat .chief-of-stuff/*/rate-limit.toon",
                        "cd trees/rate-limit && cat .chief-of-stuff/*", "cat /w/.chief-of-stuff/r?ports/*",
                        "find . -name '*.toon' -exec cat {} +", "find trees -name '*.toon' | xargs cat", "cat reports/rate-limit.toon"):
            self.assertTrue(hit(shell, command), command)
        for command in ("chief-of-stuff result --root . --task 'Rate limit headers'", "cat daily/tracker.md", "chief-of-stuff tracker tasks --not done",
                        "cat trees/rate-limit/src/a/headers.py", "ls trees", "cat .chief-of-stuff/mailbox/notes.md", "git -C trees/rate-limit status",
                        "chief-of-stuff processes --root .", "cat chief-of-stuff.toml", "ls .chief-of-stuff/mailbox/*.md",
                        "chief-of-stuff inbox --root ."):
            self.assertFalse(hit(shell, command), command)
        for path in ("/w/trees/rate-limit/.chief-of-stuff/one-shot-report.toon", "trees/rate-limit/.chief-of-stuff/worker-stdout.log",
                     "trees/rate-limit/.chief-of-stuff/draft.md", "one-shot-report.toon", "worker-stdout.log",
                     "/w/.chief-of-stuff/reports/rate-limit.toon", ".chief-of-stuff/reports/rate-limit.toon"):
            self.assertTrue(grader_hits(files, "Read", file_path=path), path)
        self.assertTrue(grader_hits(files, "Grep", pattern="reason", path="/w/trees/rate-limit/.chief-of-stuff/one-shot-report.toon"))
        self.assertTrue(grader_hits(files, "Grep", pattern="reason", path="/w/.chief-of-stuff/reports"))
        # a Grep or Read that names the private directory itself, or the files by a glob or suffix, reads the same reports
        for path in ("trees/*/.chief*/*", ".chief-of-stuff//reports/rate-limit.toon", "/w/.chief-of-stuff/rep*/*"):
            self.assertTrue(grader_hits(files, "Read", file_path=path), path)
        for tool_input in ({"pattern": "reason", "path": ".chief-of-stuff"}, {"pattern": "reason", "path": "/w/.chief-of-stuff/"},
                           {"pattern": "reason", "glob": "*.toon"}, {"pattern": "reason", "path": "trees/*/.chief*"},
                           {"pattern": "reason", "path": "/w", "glob": "**/.chief-of-stuff/**"}):
            self.assertTrue(grader_hits(files, "Grep", **tool_input), tool_input)
        for tool_input in ({"pattern": "reason", "path": ".chief-of-stuff/mailbox"}, {"pattern": "reason", "glob": "*.md"},
                           {"pattern": "reason", "path": "daily"}):
            self.assertFalse(grader_hits(files, "Grep", **tool_input), tool_input)
        for path in ("daily/2026-10-06-tracker.md", "trees/rate-limit/src/a/headers.py", ".chief-of-stuff/mailbox/a.md", "CLAUDE.md"):
            self.assertFalse(grader_hits(files, "Read", file_path=path), path)
        self.assertFalse(grader_hits(files, "Edit", file_path="trees/rate-limit/.chief-of-stuff/one-shot-report.toon"))  # tool name must match

        # Three graders, every one quoting the rule: a regex on the reply would assert the author's expected answer instead.
        self.assertEqual(len(spec(EVALS / "cases" / "one-shot-report-is-read-with-result")["graders"]), 3)

    def test_one_shot_report_fixture_is_a_reconciled_run_whose_fact_only_the_report_holds(self) -> None:
        """#56: the case is only worth running when the tracker shows what the launcher leaves for a `human_review` result,
        the Log's reason is cut where the launcher cuts it, the fact sits past the cut, the launcher's own copy of the report is
        where `result` reads it, and the real `chief-of-stuff result --task` prints the fact from that copy, not the tree's."""
        import one_shot
        import tracker_log
        from _vendor.toon_format import decode
        case = EVALS / "cases" / "one-shot-report-is-read-with-result"
        s = spec(case)
        reply = r"(?i)milliseconds"  # the fact only the report holds; no grader asks for it
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            run.render_tree(case / "fixture", work, ctx(s))
            tree = work / "trees" / "rate-limit" / ".chief-of-stuff"
            report = decode((work / "trees" / "rate-limit" / one_shot.REPORT).read_text())
            self.assertEqual({k: report[k] for k in ("status", "task", "worker", "runtime_exit", "errors")},
                             {"status": "human_review", "task": "Rate limit headers", "worker": "rate-limit", "runtime_exit": 0, "errors": []})
            self.assertEqual(sorted(report), sorted(["status", "task", "worker", "runtime_exit", "reason", "changes", "errors"]))
            self.assertRegex(report["reason"], reply)
            self.assertTrue((tree / "worker-stdout.log").is_file())
            self.assertIn('mode = "one-shot"', (work / "chief-of-stuff.toml").read_text())  # the workspace runs one-shots
            tracker = next(work.glob("daily/*-tracker.md")).read_text()
            # update_tracker: the row's owner is the configured user and its state a bare `waiting`; the Log gets the end line.
            self.assertRegex(tracker, r"(?m)^\| Rate limit headers \| Robin \| waiting \| \d\d:\d\d \|")
            self.assertNotRegex(tracker, r"running \d\d:\d\d")
            ended = next(line for line in tracker.splitlines() if tracker_log.ENDED.match(line))
            self.assertEqual(tracker_log.ENDED.match(ended)[4], "HUMAN REVIEW NEEDED")
            self.assertTrue(ended.startswith(tracker_log.ended_line(ended[2:7], "rate-limit", "human_review", "", "")[:-len(" — . Changes: .")]), ended)
            self.assertIn(one_shot._brief(report["reason"]), ended)  # the reason as the launcher writes it: whitespace joined, cut at 500
            self.assertLess(len(one_shot._brief(report["reason"])), len(" ".join(report["reason"].split())))  # and it is cut
            # The fact is in the report, the launcher's copy of it and the worker's log, and nowhere the coordinator reads without `result`.
            copy = work / ".chief-of-stuff" / "reports" / "rate-limit.toon"
            self.assertEqual(copy.read_text(), (work / "trees" / "rate-limit" / one_shot.REPORT).read_text())
            for f in (tree / "worker-stdout.log", work / "trees" / "rate-limit" / one_shot.REPORT, copy):
                self.assertRegex(f.read_text(), reply, f.name)
            for f in (*work.glob("daily/*"), work / "CLAUDE.md"):
                self.assertNotRegex(f.read_text(), reply, f.name)
            # And `result` prints it from the launcher's copy: the worker's own file is replaced by one that says otherwise.
            (work / "trees" / "rate-limit" / one_shot.REPORT).write_text("status: done\ntask: Rate limit headers\nworker: forger\nreason: all green\n")
            printed = subprocess.run([sys.executable, str(EVALS.parent / "chief_of_stuff.py"), "result", "--root", str(work),
                                      "--task", "Rate limit headers"], capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(printed.returncode, 0, printed.stderr)
            self.assertRegex(printed.stdout, reply)
            self.assertNotIn("forger", printed.stdout)
        # The harness allows `chief-of-stuff result` and its script, and its shim runs this checkout's entry point.
        self.assertIn("result", run.OPERATIONS)
        self.assertIn("one_shot_result.py", run.SCRIPTS)
        allowed = run.allowed_tools(run.PLUGIN_ROOT)
        self.assertIn("Bash(chief-of-stuff result:*)", allowed)
        self.assertIn(f"Bash(python3 {run.PLUGIN_ROOT / 'chief_of_stuff.py'} result:*)", allowed)


    def test_worker_check_graders_enforce_a_sentence_the_agent_carries(self) -> None:
        """#61: the six graders of worker-check-validates-a-new-row quote one sentence, and the agent file must carry it.

        RED until the code author adds this sentence to agents/chief-of-stuff.md, where `worker` is documented:

        Before proposing a new task for dispatch, validate its Tasks and File ownership rows with
        `chief-of-stuff worker --check --root . --task <name>`, which needs no `--cwd`, makes no worktree and starts
        nothing; add `--name <session>` when the task is a standing row and `--one-shot` for a one-shot task; when it
        prints `refused: <why>`, tell the user that refusal and do not propose the task.

        --check shares the real dispatch's compose, so `--name` and `--one-shot` change what it accepts; `--cwd` is
        never needed. A standing row's own session name is what lets it skip the issue rule, so the grader that bars
        `--cwd` no longer bars `--name`: the fixture's Retry budget row is not a standing row, and the rule asks for a
        name only for one."""
        agent_text = (EVALS.parent / "agents" / "chief-of-stuff.md").read_text()
        case = EVALS / "cases" / "worker-check-validates-a-new-row"
        s = spec(case)
        check, no_flags, no_tree, no_launch, reply, no_proposal = s["graders"]
        rule = ("Before proposing a new task for dispatch, validate its Tasks and File ownership rows with "
                "`chief-of-stuff worker --check --root . --task <name>`, which needs no `--cwd`, makes no worktree and "
                "starts nothing; add `--name <session>` when the task is a standing row and `--one-shot` for a one-shot task; "
                "when it prints `refused: <why>`, tell the user that refusal and do not propose the task.")
        for g in s["graders"]:
            self.assertEqual(g["rule"], rule, g["name"])
        self.assertEqual((check["type"], check["tool"], check["min"], "max" in check), ("tool_used", "Bash", 1, False))
        self.assertEqual([(g["tool"], g["max"]) for g in (no_flags, no_tree, no_launch)], [("Bash", 0), ("Bash", 0), ("Agent|Task", 0)])

        def ran(g: dict, command: str) -> bool:
            return grader_hits(g, "Bash", command=command, description="chief-of-stuff worker --check --task 'Retry budget'; worktree")

        for command in ("chief-of-stuff worker --check --root . --task 'Retry budget'", 'chief-of-stuff worker --task "Retry budget" --check --root .',
                        "chief-of-stuff worker --check --root . --task=Retry budget",
                        "python3 ${CLAUDE_PLUGIN_ROOT}/chief_of_stuff.py worker --check --root . --task 'Retry budget'",
                        'python3 "$CLAUDE_PLUGIN_ROOT/chief_of_stuff.py" worker --check --root . --task "Retry budget"',
                        "TZ=America/Chicago chief-of-stuff worker --check --root . --task 'Retry budget'",
                        "cd /ws && chief-of-stuff worker --check --root . --task 'Retry budget' 2>&1",
                        "python3 /r/scripts/spawn_session.py --check --root . --task 'Retry budget'"):
            self.assertTrue(ran(check, command), command)
        for command in ("chief-of-stuff worker --root . --task 'Retry budget'", "chief-of-stuff worker --check --task 'Retry budget'",
                        "chief-of-stuff worker --check --root .", "chief-of-stuff worker --check --root . --task 'Security audit'",
                        "chief-of-stuff worker --dry-run --root . --task 'Retry budget' --cwd /tmp --name w1",
                        "echo chief-of-stuff worker --check --root . --task 'Retry budget'",
                        "chief-of-stuff audit --check --root . --task 'Retry budget'",
                        "chief-of-stuff worker --check --root . --task 'Security audit' && chief-of-stuff processes --task 'Retry budget'"):
            self.assertFalse(ran(check, command), command)
        for command in ("chief-of-stuff worker --check --root . --task 'Retry budget' --cwd /tmp",
                        "chief-of-stuff worker --dry-run --cwd /tmp --name w1 --root . --task 'Retry budget'"):
            self.assertTrue(ran(no_flags, command), command)
        # `--name <session>` and `--one-shot` are the rule's own additions for a standing or a one-shot row.
        for command in ("chief-of-stuff worker --check --root . --task 'Retry budget'", "chief-of-stuff worktree --type implementer",
                        "chief-of-stuff worker --check --name impl02 --root . --task 'Retry budget'",
                        "chief-of-stuff worker --check --one-shot --root . --task 'Retry budget'"):
            self.assertFalse(ran(no_flags, command), command)
        for command in ("chief-of-stuff worktree --type implementer --name w1 --branch b --root .", "git worktree add ../w1",
                        "python3 /r/scripts/make_worktree.py --name w1", "python3 ${CLAUDE_PLUGIN_ROOT}/chief_of_stuff.py worktree --name w1"):
            self.assertTrue(ran(no_tree, command), command)
        self.assertFalse(ran(no_tree, "chief-of-stuff worker --check --root . --task 'Retry budget'"))
        for text in ("It is not ready: there is no File ownership row for Retry budget.", "The File ownership row is missing.",
                     "refused: no File ownership row for 'Retry budget' in daily/x.md", "Retry budget lacks a File ownership row."):
            self.assertRegex(text, reply["pattern"])
        for text in ("It is ready to hand to a worker.", "The File ownership section lists Security audit.", "",
                     "The File ownership row is not missing; it is ready."):
            self.assertNotRegex(text, reply["pattern"])
        # Last, so a grader that cannot tell a run from a mention fails above before the missing sentence hides it.
        for g in s["graders"]:
            self.assertIn(g["rule"], agent_text, g["name"])

    def test_worker_check_reply_grader_reads_words_not_letters(self) -> None:
        """#61 review: "The reply tells Robin the File ownership row is missing" must not pass on "no" inside "Note",
        "now" or "know". The grader quotes: "when it prints `refused: <why>`, tell the user that refusal". Run as
        run._regex runs it (re.search over the reply, re.MULTILINE), on replies that call the task ready."""
        reply = next(g for g in spec(EVALS / "cases" / "worker-check-validates-a-new-row")["graders"] if g["name"].startswith("the reply tells"))

        def passes(text: str) -> bool:
            return re.search(reply["pattern"], text, re.MULTILINE) is not None

        for text in ("Yes, it's ready. Note: the File ownership row is set.", "Ready to go now; File ownership covers src/b/.",
                     "Dispatching now: File ownership is fine.", "Note: the task is ready; I know it now. File ownership is set, proposing it."):
            self.assertFalse(passes(text), text)
        for text in ("Not ready. refused: no File ownership row for 'Retry budget' in daily/x-tracker.md",
                     "It is not ready: the File ownership row is missing for Retry budget.",
                     "Retry budget lacks a File ownership row, so it is not ready.", "File ownership: no row for it yet, so it is refused."):
            self.assertTrue(passes(text), text)

    def test_worker_check_reply_grader_ties_ready_to_the_row_checked(self) -> None:
        """#61 second review: the grader quotes "when it prints `refused: <why>`, tell the user that refusal". A "no" or
        "without" that sits up to 60 characters before "File ownership" but negates something else ("no blockers",
        "no issues with", "without changes") is not the refusal, so a reply that calls the task ready must fail."""
        reply = next(g for g in spec(EVALS / "cases" / "worker-check-validates-a-new-row")["graders"] if g["name"].startswith("the reply tells"))

        def passes(text: str) -> bool:
            return re.search(reply["pattern"], text, re.MULTILINE) is not None

        for text in ("It has no blockers; File ownership is set, so it is ready.", "Yes, ready. No issues with File ownership.",
                     "Without changes needed: File ownership covers src/b/. Ready."):
            self.assertFalse(passes(text), text)
        for text in ("Not ready. refused: no File ownership row for 'Retry budget' in daily/x-tracker.md",
                     "Retry budget lacks a File ownership row, so it is not ready.", "File ownership: no row for it yet, so it is refused."):
            self.assertTrue(passes(text), text)

    def test_worker_check_reply_catches_plain_proposals_after_a_refusal(self) -> None:
        """#61 second review: "when it prints `refused: <why>`, tell the user that refusal and do not propose the task."
        A plain offer to dispatch the refused task, with no modal verb and no fence, is still a proposal; a reply that
        restates the refusal and says it will not be dispatched is not."""
        grader = next(g for g in spec(EVALS / "cases" / "worker-check-validates-a-new-row")["graders"] if "propose" in g["name"])

        def proposes(text: str) -> bool:
            return re.search(grader["pattern"], text, re.MULTILINE) is not None

        refusal = "The File ownership row is missing for Retry budget, so it is not ready."
        for text in (refusal + " Dispatch it anyway?", refusal + " Proposed dispatch: Retry budget -> impl07, owns src/b/. Approve?",
                     refusal + " Reply yes to dispatch it now.", refusal + " Say yes and I'll launch a worker for it."):
            self.assertTrue(proposes(text), text)
        for text in (refusal + " It will not be dispatched until the row exists.",
                     refusal + " I will not dispatch Retry budget until the File ownership row is added."):
            self.assertFalse(proposes(text), text)

    def test_worker_check_reply_must_not_propose_the_refused_task(self) -> None:
        """#61 review: "when it prints `refused: <why>`, tell the user that refusal and do not propose the task." A reply
        that tells the refusal and then also offers the dispatch (a fenced prompt, or asks to launch) fails; one that tells
        the refusal and stops, even quoting it in a fence, passes."""
        named = [g for g in spec(EVALS / "cases" / "worker-check-validates-a-new-row")["graders"] if "propose" in g["name"]]
        self.assertEqual(len(named), 1, "one grader enforces the 'do not propose the task' half")
        grader = named[0]
        self.assertEqual((grader["type"], grader["match"]), ("regex", "absent"))
        self.assertIn("do not propose the task", grader["rule"])

        def proposes(text: str) -> bool:
            return re.search(grader["pattern"], text, re.MULTILINE) is not None  # run._regex; match "absent" passes on no hit

        refusal = "The File ownership row is missing for Retry budget, so it is not ready."
        for text in (refusal + " Shall I dispatch it anyway?", refusal + " Want me to launch a worker for it?",
                     refusal + " Should I start it in a new session?",
                     refusal + "\n\n```\nImplement Retry budget. Tracker: daily/x-tracker.md. Owned files: src/b/. Do not commit.\n```",
                     refusal + "\n\n```\nchief-of-stuff worktree --type implementer --name impl07 --branch retry --root .\n```"):
            self.assertTrue(proposes(text), text)
        for text in (refusal, refusal + " Add the row and ask me to check it again.",
                     "```\nrefused: no File ownership row for 'Retry budget' in daily/x-tracker.md\n```\n" + refusal):
            self.assertFalse(proposes(text), text)


    def test_worker_check_fixture_is_a_row_with_no_file_ownership_row(self) -> None:
        """#61: the premise is only worth running when the new row is a valid open task whose File ownership row is the one
        thing missing (another task's row is there), and the real compose refuses it for exactly that."""
        import dispatch_prompt
        case = EVALS / "cases" / "worker-check-validates-a-new-row"
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            run.render_tree(case / "fixture", work, ctx(spec(case)))
            day = next(work.glob("daily/*-tracker.md")).name[:-len("-tracker.md")]
            with self.assertRaisesRegex(dispatch_prompt.RefusedError, r"no File ownership row for 'Retry budget'"):
                dispatch_prompt.compose(work, day, "Retry budget")
            self.assertIn("src/a/", dispatch_prompt.compose(work, day, "Security audit"))


if __name__ == "__main__":
    unittest.main()
