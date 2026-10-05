"""A lint over every eval case: the fixture shapes that rewarded the coordinator for doing less.

Four shapes in one day (findings 65, 68, 74): a repo with no files, a fixture with no `CLAUDE.md`, a
Sessions table the ruleset had stopped writing, a `- Verified:` line with no clock. Every one passed
every unit test, because no test read the cases. This one does, and it runs under CIMP on main.
"""

import contextlib
import io
import json
import re
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

    def test_one_shot_in_flight_graders_enforce_sentences_the_agent_carries(self) -> None:
        """#53: a running one-shot is neither launched again (the readiness sentence) nor looked up with `ps`/`pgrep` (the helper
        sentence). The launch regex needs a `worker` call whose `--task` is the running task, not any worker call; the
        liveness regex reads the command field for `ps`, `pgrep` or `pkill` as a word, not inside `chief-of-stuff processes`."""
        agent_text = (EVALS.parent / "agents" / "chief-of-stuff.md").read_text()
        launch, listing = spec(EVALS / "cases" / "one-shot-in-flight-is-checked-with-processes")["graders"]
        self.assertIn("Ready means an open, unassigned task", launch["rule"])
        self.assertIn("Use this helper instead of ad hoc `ps` or `pgrep` calls", listing["rule"])
        for g in (launch, listing):
            self.assertIn(g["rule"], agent_text, g["name"])
            self.assertEqual((g["type"], g["tool"], g["max"]), ("tool_used", "Bash", 0))

        def hit(g: dict, command: str) -> bool:
            return re.search(g["input_match"], json.dumps({"command": command, "description": "chief-of-stuff worker --task 'Rate limit headers'"})) is not None

        for command in ("chief-of-stuff worker --task 'Rate limit headers' --dry-run", 'chief-of-stuff worker --task "Rate limit headers"',
                        "python3 /release/chief_of_stuff.py worker --runtime codex --task 'Rate limit headers' --dry-run",
                        "python3 /release/scripts/spawn_session.py --task=Rate limit headers"):
            self.assertTrue(hit(launch, command), command)
        for command in ("chief-of-stuff worker --task 'Draft release notes' --dry-run", "chief-of-stuff processes --root .",
                        "echo chief-of-stuff worker --task Other", "cat daily/tracker.md # Rate limit headers"):
            self.assertFalse(hit(launch, command), command)
        for command in ("pgrep -fl trees/rate-limit", "ps aux | grep rate-limit", "ps -p 1", "cd trees && ps -ef", "pgrep -f x | wc -l",
                        "echo $(ps -o pid= -p 1)", "/bin/ps -ef", "pkill -0 -f trees/rate-limit", "ls trees; pgrep codex"):
            self.assertTrue(hit(listing, command), command)
        for command in ("chief-of-stuff processes --root .", "python3 /release/scripts/process_status.py --root .",
                        "chief-of-stuff worker --task 'Rate limit headers' --dry-run", "grep -rn steps notes/", "cat trees/rate-limit/.chief-of-stuff/one-shot.pid",
                        "cat daily/tracker.md; ls maps", "chief-of-stuff tracker tasks --not done"):
            self.assertFalse(hit(listing, command), command)

    def test_one_shot_in_flight_fixture_is_a_run_the_launcher_wrote_and_processes_lists(self) -> None:
        """#53: the case is only worth running when `processes` really prints the one-shot. Rendered the way the harness
        renders it, pid 1 (alive on every host) and the pid file's task come back as the one-shot row, and the tracker's
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
            rows = decode(out.getvalue())
            self.assertEqual([(r["pid"], r["task"], Path(r["worktree"]).name, r["status"], r["kind"]) for r in rows],
                             [(1, "Rate limit headers", "rate-limit", "running", "one-shot")])
            tracker = next(work.glob("daily/*-tracker.md")).read_text()
            started = tracker_log.started_line("00:00", "rate-limit", "codex", "gpt-5.1-codex", "trees/rate-limit", "Rate limit headers")
            self.assertIn(started.split(" ", 2)[2], tracker)
            self.assertRegex(tracker, r"(?m)^\| Rate limit headers \| rate-limit \| running \d\d:\d\d \|")


if __name__ == "__main__":
    unittest.main()
