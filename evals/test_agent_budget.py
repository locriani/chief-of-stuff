"""A byte budget for agents/chief-of-stuff.md and the prompts rendered from it (#407).

The agent file is loaded whole at every session start, so its size is a recurring cost. These are
ceilings, not targets.
"""

import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import runtimes  # noqa: E402
import start_coordinator as start  # noqa: E402
from evals.rules_text import one_shot_paragraph, rules_text, sections, skill_body  # noqa: E402
from evals.skill_fixtures import write_plugin, write_skill  # noqa: E402

# Each later slice lowers these in a red commit before it trims the text.
# Slice 4 (#488): move Tasks-row column rules to tracker-rows; keep state,
# done evidence, ownership and Open the day widening rules in the agent.
TOTAL = 72_000            # whole agent file, bytes
MAX_LINE = 8_400          # longest single line
ONE_SHOT_PARAGRAPH = 4_400  # kept: a later slice trims this paragraph
# Skills are read on demand; no host's prompt appends their bodies.
# rendered_sizes(1), with release/workspace paths removed: Claude 71,453 B,
# Codex 63,975 B, Cursor 64,083 B, AGY 64,139 B; each ceiling rounds up to 50 B.
PROMPT = {"claude": 71_500, "codex": 64_000, "cursor": 64_100, "agy": 64_150}  # rendered, path bytes removed
CEILING = {  # `## ` section -> bytes, heading line included
    "Role": 1600, "Dispatch authority": 1100, "Browser safety": 410, "Writing": 780,
    "Config": 1400, "Clock": 1850, "Calendar": 910, "Open the day": 2000, "Resume": 4830,
    "Write authority": 2310, "Filing": 360, "Human-only actions": 1280, "Dispatch": 15210,
    "Assign": 2560, "Brief": 730, "Pipeline": 6150, "Triage": 3770, "Check": 2780,
    "Sessions": 7380, "Relay": 2510, "Tracker": 5_500, "Notices": 740, "Board": 1750,
    "Requirements": 360, "Share": 460, "Asks": 4700, "Notify": 360,
}
SKILL_CEILING = {"decision-page": 2_700, "optional-features": 5_200, "tracker-rows": 5_400}  # moved bytes + metadata/organization/headroom
SKILLS_TOTAL = 13_300  # all SKILL.md files, bytes
DESCRIPTION_CAP = 300  # frontmatter description characters, not bytes
SKILL_POINTER = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/skills/([^/\s`]+)/SKILL\.md")
DECISION_PAGE_POINTER = "Before writing a decision page, Read ${CLAUDE_PLUGIN_ROOT}/skills/decision-page/SKILL.md"
OPTIONAL_FEATURE_POINTERS = {
    "Filing": "Before moving a file into a PARA home, Read ${CLAUDE_PLUGIN_ROOT}/skills/optional-features/SKILL.md",
    "Requirements": "Before ticking or editing a requirements file, and when a task goes done and the Coordinator block names one, Read ${CLAUDE_PLUGIN_ROOT}/skills/optional-features/SKILL.md",
    "Notify": "When the Coordinator block has a Settings line, Read ${CLAUDE_PLUGIN_ROOT}/skills/optional-features/SKILL.md",
}
OPTIONAL_FEATURE_RULES = {
    "Filing": (
        "A move is `mv`, nothing else.",
        "Never copy, delete, rename, or edit what you move.",
        "Never move anything outside the workspace root unless the user has asked for it.",
    ),
    "Requirements": (
        "Your only edit is a tick: flip `- [ ]` to `- [x]` and append ` — evidence: <commit, URL, or Log HH:MM>`, with Edit.",
        "Never add, remove, reorder, or reword an item, never untick one, and never tick without evidence.",
        "A task that matches no item ticks nothing.",
    ),
    "Notify": (
        "Notifications are off by default, including an empty `[notify]` table.",
        "Never run routine `notify sync` or edit the queue yourself.",
        "Once per ask, in the same move as the ask.",
    ),
}

TRACKER_ROWS_POINTER = "Before writing or changing a Tasks row, filing or closing its issue, or moving its stage, Read ${CLAUDE_PLUGIN_ROOT}/skills/tracker-rows/SKILL.md"
# Verbatim sentences from the Tasks bullet, pinned before slice 4 moves them.
TRACKER_ROW_RULES = {
    'pipe and name': (
        'A `|` inside any cell is written `\\|`, backticks or not: a raw one is a column delimiter, and the board reads every column right of it one place over.',
        '`name` is what the task is called: a short noun phrase, yours to write when you write the task and to rewrite when the item changes, and never more than a line.',
        'It is the label the board draws for a task; `item` keeps the wording, the history you append, and the status note, and is read only when someone opens the task.',
        'Write the name because you know which words are the task and which are its history — the board cannot tell, and before this column it guessed by splitting the item cell on its first sentence, which is why tasks drew as `**Deploy main.**…` with the asterisks showing.',
        'A task whose row has no name still draws: the board falls back to that guess, so an older tracker keeps working and is not to be rewritten wholesale to add the column.',
    ),
    'sha': (
        "The optional `<sha>` is the commit that carried THIS task's change onto main — the one you already reported in `Verified`, written here so a machine can read it.",
        "`audit_tasks.py` clears a task from `git merge-base --is-ancestor <sha> main` when it is there, and falls back to asking whether the owner's TREE is on main when it is not; a sha it cannot parse or cannot find is treated as absent, never as a clear.",
        'Write it only when you have it, and never one you did not verify: a task reopened in error is noisy and self-clearing, and a task cleared in error is silent and permanent.',
    ),
    'size': (
        '`size` is your judgement of the item as written, `S`, `M`, `L` or `XL`, made when you write the task and remade when the item changes: `S` is one edit, one file, one fact to check; `M` is one item a session finishes in a sitting, a few files, one suite run; `L` is a plan with bullets, several files, its own pull request; `XL` is a task that spawns other tasks or spans sessions.',
        'It is not a guess at hours — the board measures those from closed tasks of each size — and a value a person wrote stands.',
        'A size arrives when you write the row or its item changes, never in a move about another task: a tracker you inherit with six columns is widened, with every size blank, at Open the day, and until then it is read as it is — the board parses each width.',
    ),
    'due, issue, lane and stage': (
        "`since` and `due` are what the board draws from: `due` is `HH:MM`, a date, a deadline name from the block, or blank: a blank `due` leaves the board to derive an end from the owner's queue and draw the remaining stages as forecast; it stays blank until a person fills it, and you never fill it in to make the board look complete.",
        '`issue` is the issue that backs the task, when the `## Coordinator` block has a `Backlog:` line: `#N` in that backlog, `owner/repo#N` on GitHub, or the issue\'s URL (GitHub or GitLab) (the user, 2026-09-22 22:20: "each entry in the task tracker is actually backed by an entry in github").',
        'A workflow step — a review, or anything the pipeline itself performs — takes no issue: write `workflow` in its `issue` cell.',
        "A review you are asked to do is a workflow step with its own `workflow` row; a review stage on a task is a change of state on that task's own issue.",
        'Otherwise file it **before** you write the row.',
        'On GitLab (the user, 2026-09-23 22:40: "make everything use gitlab now that we have that going"): `chief-of-stuff backlog --create <the name> --body <outcome, why, acceptance> --commit`, which prints the new number.',
        'On GitHub: `chief-of-stuff backlog --create <the name> --body <outcome, why, acceptance> --commit`; use `--parent <issue-number>` or repeated `--blocked-by <issue-number>` for native relationships rather than writing issue numbers in prose.',
        'The body goes to `gh` on stdin, and the created issue number is printed.',
        'Writing `done` on a task closes its issue in the same move: `chief-of-stuff backlog --close N --commit`, and one Log line.',
        "A standing session's placeholder is not a task and takes no issue, and neither does a row that went `done` before the rule.",
        'A change of state — a kanban stage, a hold label, a review or merge step — is recorded on the issue it concerns, in the stage column and the Log, never as a new issue.',
        '`lane` names a lane from the `[lanes]` table of the block\'s `Settings:` file, and `stage` is where the task stands in it (the user, 2026-09-23: "A lane: the sequence that a task has to move through").',
        'A lane is its stages in order; a gate is a stage only the user\'s word passes, and the board marks a task at one "waiting on you".',
        'A task with no tree (a decision, a relay, a console action) leaves both cells blank and draws no card in BUILD.',
        'Move `stage` with `chief-of-stuff log --root . --stage "<name>" <stage>` when the task moves, once for every stage it enters, even two in one move: it sets the cell and writes the `stage: <name> → <stage>` Log line the board\'s Flow charts read, under the tracker lock.',
        'Never set the cell by hand or write that line as free text.',
    ),
}
TRACKER_RETAINED_RULES = (
    '- **Tasks**: one row per item: `| name | item | owner | state | since | due | size | lane | stage | issue | checklist |`.',
    "Owner is the user's name, a working session, a dispatched context, or `unassigned`.",
    'State is one of `open`, `running HH:MM`, `waiting`, `orphaned`, `done`, `done HH:MM`, `done HH:MM–HH:MM`, `done HH:MM–HH:MM <sha>`; no other words.',
    '`done` keeps the running start — `running 21:16` closes as `done 21:16–22:05`.',
    'The start is the one duration the tracker ever records, and `done 22:05` alone throws it away.',
    '`unassigned` is not the user: an item is theirs only when they took it, and only an `unassigned` task may be proposed for dispatch or assignment.',
    "A task is `done` only when its change is integrated into main **and the suite has been run on main after integration**: work sitting on a branch, in a worktree, in an open pull request, or uncommitted is `waiting`, however green its suites, and merged-but-untested-on-main is `waiting` too — except a task closed because its issue closed on the forge, `done` at the close time regardless, since the forge closure is someone's decision that the task is over.",
    "An open pull request's URL goes in the item when `[workflow] delivery` is `pull-request`.",
    "The configured delivery and merge owner govern how code reaches main, and the reason the suite runs on main after it is that a merge can break main without either side's branch suite noticing.",
    'No script can watch a suite run, so the evidence is a claim with a citation: the owner reports the suite result and the commit main was at, and that sha goes in `Verified`.',
    "`audit_tasks.py` prints main's current sha on every run — when it no longer matches the one in `Verified`, the evidence is about a main that no longer exists and the task is not closed on it.",
    'Before writing `done` on a task whose File ownership names a worktree, run `chief-of-stuff audit --date <today>`, check `chief-of-stuff inbox list --recipient coordinator --unread` for any pending worker reports or stops on that task, and believe the audit and verified evidence over any unverified claim.',
    'Tasks with no tree — a decision, a relay, a deletion, a console action — are unaffected: there is nothing to merge, and the rule never sends you looking.',
    "A task whose File ownership names no worktree closes on its owner's or its reporter's word; going through the workspace for a file a session says it wrote is checking their work, which is not yours to do.",
    'When that word names a sha as on main, check it first: `chief-of-stuff audit --sha <sha>` fetches and prints whether origin/main or local main holds it, and a sha it finds on neither closes nothing.',
    'Main not yet pushed is a `Verified` fact, not a state.',
    "A task going to `done` ticks its checklist item in today's log.",
    'A tracker with eight columns is widened to nine at Open the day, the `issue` cell blank until it is filed.',
    'A tracker with nine columns is widened to eleven at Open the day, both cells blank.',
)
TRACKER_DELETED_RULES = (
    "`audit_tasks.py` reads every issue's state and reports an open task with no issue, a cell that is not an issue or `workflow`, an issue that is closed or missing under an open task, and an issue still open under a done one; its `issue: unknown` line is a check that did not run, never a pass.",
    "`audit_tasks.py` reports a lane the settings file does not name, a stage that is not one of its lane's, a lane with no stage, and a stage with no lane.",
)


def rendered_sizes(pad: int, source: Path = ROOT) -> dict:
    """Each host's prompt size with the release and workspace paths taken out, so a short CI path
    and a long checkout path budget the same. `pad` lengthens both paths."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / ("w" * pad) / "workspace"
        root.mkdir(parents=True)
        release = start.install(source, base / ("i" * pad))
        sizes = {}
        for runtime in runtimes.NAMES:
            text = start.prompt(runtime, release, root).replace(str(release), "").replace(str(root), "")
            sizes[runtime] = len(text.encode())
        return sizes


def assert_skill_budgets(test: unittest.TestCase, root: Path, ceilings: dict, total: int):
    found = {p.parent.name: len(p.read_bytes()) for p in sorted((root / "skills").glob("*/SKILL.md"))}
    test.assertEqual(set(found), set(ceilings), "each skill needs an explicit budget")
    for name, size in found.items():
        test.assertLessEqual(size, ceilings[name], f"skill {name} exceeds its byte ceiling")
    test.assertLessEqual(sum(found.values()), total, "skills exceed their total byte ceiling")


def description(text: str) -> str:
    """Read the description key only in leading frontmatter, including folded/literal prose.

    Skill descriptions use plain or quoted strings, or YAML's indented block notation.
    For budgeting, join nonempty scalar lines with one space, including literal blocks.
    Searching the whole file would count an example's description as metadata.
    """
    front = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", text, flags=re.S)
    if not front:
        return ""
    lines = front[1].splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^description:[ \t]*(.*)$", line)
        if not match:
            continue
        value = match[1].strip()
        parts = [] if value in ("", ">", ">-", ">+", "|", "|-", "|+") else [value]
        for continuation in lines[index + 1:]:
            if not continuation.strip():
                continue
            if not continuation.startswith((" ", "\t")):
                break
            parts.append(continuation.strip())
        return " ".join(parts).strip("\"'")
    return ""


def assert_skill_descriptions(test: unittest.TestCase, root: Path):
    for path in sorted((root / "skills").glob("*/SKILL.md")):
        test.assertLessEqual(len(description(path.read_text())), DESCRIPTION_CAP,
                             f"skill {path.parent.name} description exceeds {DESCRIPTION_CAP} characters")


def assert_skill_pointers(test: unittest.TestCase, root: Path):
    text = (root / "agents" / "chief-of-stuff.md").read_text()
    for name in SKILL_POINTER.findall(text):
        test.assertTrue((root / "skills" / name / "SKILL.md").is_file(),
                        f"agent pointer names missing skill {name}")


class AgentBudgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw = (ROOT / "agents" / "chief-of-stuff.md").read_bytes()  # bytes: CRLF or locale cannot hide growth
        cls.size = len(raw)
        cls.text = raw.decode()

    def test_total(self):
        self.assertLessEqual(self.size, TOTAL)

    def test_each_section_is_within_its_ceiling(self):
        found = sections(self.text)
        self.assertEqual(set(CEILING), set(found), "a new section needs a budget line, a removed one loses its line")
        for name, text in found.items():
            with self.subTest(section=name):
                self.assertLessEqual(len(text.encode()), CEILING[name])

    def test_a_fenced_heading_is_not_a_section(self):
        self.assertEqual(sections("## A\nx\n```\n## B\n```\ny\n"), {"A": "## A\nx\n```\n## B\n```\ny\n"})
        with self.assertRaises(ValueError):
            sections("## A\n## A\n")
        # The Resume code fence holds a literal `## Resume` line; a naive parse restarts the count there.
        self.assertGreater(len(sections(self.text)["Resume"].encode()), 4000)

    def test_no_line_outgrows_the_cap(self):
        for line in self.text.split("\n"):
            self.assertLessEqual(len(line.encode()), MAX_LINE, line[:60])

    def test_the_one_shot_paragraph_is_capped(self):
        self.assertLessEqual(len(one_shot_paragraph(rules_text()).encode()), ONE_SHOT_PARAGRAPH)

    def test_every_hosts_rendered_prompt_is_capped_whatever_the_path_length(self):
        short, long = rendered_sizes(1), rendered_sizes(40)
        self.assertEqual(short, long)
        self.assertEqual(set(PROMPT), set(short))
        for runtime, size in short.items():
            with self.subTest(runtime=runtime):
                self.assertLessEqual(size, PROMPT[runtime])

    def test_each_skill_and_the_total_are_within_their_byte_ceilings(self):
        assert_skill_budgets(self, ROOT, SKILL_CEILING, SKILLS_TOTAL)

    def test_every_skill_frontmatter_description_is_capped(self):
        assert_skill_descriptions(self, ROOT)

    def test_every_agent_skill_pointer_names_an_existing_skill(self):
        assert_skill_pointers(self, ROOT)

    def test_decision_page_pointer_occurs_exactly_once_at_the_point_of_use(self):
        self.assertEqual(self.text.count(DECISION_PAGE_POINTER), 1)
        self.assertIn(DECISION_PAGE_POINTER, self.text.splitlines(), "pointer must be a one-line imperative")
        self.assertIn(DECISION_PAGE_POINTER, sections(self.text)["Asks"])

    def test_decision_page_skill_exists(self):
        self.assertTrue((ROOT / "skills" / "decision-page" / "SKILL.md").is_file(),
                        "decision-page skill is missing")

    def test_optional_feature_pointers_are_the_only_content_in_their_sections(self):
        found = sections(self.text)
        for name, pointer in OPTIONAL_FEATURE_POINTERS.items():
            with self.subTest(section=name):
                self.assertEqual(self.text.count(pointer), 1)
                self.assertEqual(self.text.splitlines().count(pointer), 1,
                                 "pointer must be a standalone imperative with no final period")
                lines = [line for line in found[name].splitlines() if line.strip()]
                self.assertEqual(lines, [f"## {name}", pointer],
                                 "keep the heading and exactly one pointer line, with nothing else")

    def test_state_report_requirements_file_refers_to_requirements_section(self):
        authority = sections(self.text)["Write authority"]
        state_report = next(paragraph for paragraph in authority.split("\n\n")
                            if paragraph.startswith("A state report is "))
        self.assertIn("a requirements file (see Requirements)", state_report)

    def test_optional_features_skill_exists_with_portable_frontmatter(self):
        path = ROOT / "skills" / "optional-features" / "SKILL.md"
        self.assertTrue(path.is_file(), "optional-features skill is missing")
        text = path.read_text()
        front = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", text, flags=re.S)
        self.assertIsNotNone(front, "optional-features needs leading YAML frontmatter")
        self.assertRegex(front[1], r'''(?m)^name:[ \t]*(?:optional-features|"optional-features"|'optional-features')[ \t]*$''')
        self.assertNotRegex(front[1], r"(?m)^\s*hosts\s*:")
        prose = description(text)
        self.assertTrue(prose, "optional-features needs a description")
        self.assertLessEqual(len(prose), DESCRIPTION_CAP)
        for topic in (r"\bPARA filing\b", r"\brequirements checklists\b", r"\bnotifications\b"):
            with self.subTest(topic=topic):
                self.assertRegex(prose, re.compile(topic, re.I))

    def test_optional_feature_rule_sentences_are_single_sourced_in_the_skill_body(self):
        path = ROOT / "skills" / "optional-features" / "SKILL.md"
        self.assertTrue(path.is_file(), "optional-features skill is missing")
        body = skill_body(path.read_text())
        for name, sentences in OPTIONAL_FEATURE_RULES.items():
            for sentence in sentences:
                with self.subTest(section=name, sentence=sentence):
                    self.assertEqual(body.count(sentence), 1, "preserve the verbatim rule in the skill body")
                    self.assertNotIn(sentence, self.text, "moved rules must no longer live in the agent")

    def test_tracker_rows_pointer_occurs_once_as_its_own_paragraph_in_tracker(self):
        self.assertEqual(self.text.count(TRACKER_ROWS_POINTER), 1)
        self.assertEqual(self.text.splitlines().count(TRACKER_ROWS_POINTER), 1,
                         "pointer must be a standalone imperative with no final period")
        self.assertIn(TRACKER_ROWS_POINTER, sections(self.text)["Tracker"].split("\n\n"),
                      "the pointer must be its own paragraph inside Tracker")

    def test_tracker_lane_stage_fragment_is_absent_from_agent_and_rules_text(self):
        for source, text in (("agent", self.text), ("rules_text", rules_text())):
            with self.subTest(source=source):
                self.assertNotIn("New: `lane`, `stage`.", text)

    def test_tracker_rows_skill_exists_with_portable_frontmatter(self):
        path = ROOT / "skills" / "tracker-rows" / "SKILL.md"
        self.assertTrue(path.is_file(), "tracker-rows skill is missing")
        text = path.read_text()
        front = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", text, flags=re.S)
        self.assertIsNotNone(front, "tracker-rows needs leading YAML frontmatter")
        self.assertRegex(front[1], r"(?m)^name:[ \t]*(?:tracker-rows|\"tracker-rows\"|'tracker-rows')[ \t]*$")
        self.assertNotRegex(front[1], r"(?m)^\s*hosts\s*:")
        prose = description(text)
        self.assertTrue(prose, "tracker-rows needs a description")
        self.assertLessEqual(len(prose), DESCRIPTION_CAP)

    def test_tracker_row_rule_sentences_are_single_sourced_in_the_skill_body(self):
        path = ROOT / "skills" / "tracker-rows" / "SKILL.md"
        self.assertTrue(path.is_file(), "tracker-rows skill is missing")
        body, combined = skill_body(path.read_text()), rules_text()
        for opening in (
            "A `|` inside any cell is written", "A task whose row has no name still draws",
            "The optional `<sha>` is the commit", "Write it only when you have it",
            "`size` is your judgement", "A size arrives when you write the row",
            "`since` and `due` are what the board draws from",
            "Never set the cell by hand or write that line as free text",
        ):
            with self.subTest(opening=opening):
                self.assertNotIn(opening, self.text)
                self.assertIn(opening, body)
                self.assertIn(opening, combined)
        for topic, sentences in TRACKER_ROW_RULES.items():
            for sentence in sentences:
                with self.subTest(topic=topic, sentence=sentence):
                    self.assertEqual(body.count(sentence), 1, "preserve the verbatim rule in the skill body")
                    self.assertNotIn(sentence, self.text, "moved rules must no longer live in the agent")
                    self.assertIn(sentence, combined)

    def test_tracker_state_done_and_widening_rules_stay_in_the_agent(self):
        tracker = sections(self.text)["Tracker"]
        for sentence in TRACKER_RETAINED_RULES:
            with self.subTest(sentence=sentence):
                self.assertIn(sentence, tracker)

    def test_tracker_redundant_audit_sentences_are_absent_from_rules_text(self):
        text = rules_text()
        for sentence in TRACKER_DELETED_RULES:
            with self.subTest(sentence=sentence):
                self.assertNotIn(sentence, text)

    def test_skill_budget_controls_reject_unbudgeted_and_oversized_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_plugin(Path(tmp))
            path = write_skill(root, "alpha", "A generic rule: é.", "description: Use for a generic task.")
            size = len(path.read_bytes())
            self.assertGreater(size, len(path.read_text()), "budget controls exercise bytes, not characters")
            assert_skill_budgets(self, root, {"alpha": size}, size)
            with self.assertRaisesRegex(AssertionError, "explicit budget"):
                assert_skill_budgets(self, root, {}, size)
            with self.assertRaisesRegex(AssertionError, "alpha exceeds its byte ceiling"):
                assert_skill_budgets(self, root, {"alpha": size - 1}, size)
            with self.assertRaisesRegex(AssertionError, "total byte ceiling"):
                assert_skill_budgets(self, root, {"alpha": size}, size - 1)

    def test_description_controls_use_the_frontmatter_key_and_character_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_plugin(Path(tmp))
            body = "## Generic rule\n\ndescription: " + "x" * 500
            for template in ("{}", '"{}"', "'{}'", ">-\n  {}", "|-\n  {}"):
                with self.subTest(template=template):
                    # Description is the first key, so a parser expecting a preceding newline fails.
                    path = write_skill(root, "alpha", body,
                                       "description: " + template.format("é" * DESCRIPTION_CAP) + "\nname: alpha")
                    self.assertEqual(description(path.read_text()), "é" * DESCRIPTION_CAP)
                    assert_skill_descriptions(self, root)
                    write_skill(root, "alpha", body,
                                "description: " + template.format("é" * (DESCRIPTION_CAP + 1)) + "\nname: alpha")
                    with self.assertRaisesRegex(AssertionError, "alpha description exceeds 300"):
                        assert_skill_descriptions(self, root)

    def test_multiline_description_controls_count_all_400_characters(self):
        # Budget normalization joins nonempty continuation lines with one space,
        # including literal blocks; this helper measures prose, not YAML rendering.
        expected = "é" * 199 + " " + "é" * 200
        templates = {
            "plain_continuation": "description: {first}\n  {second}",
            "value_on_next_line": "description:\n  {first}\n  {second}",
            "folded_blank_line": "description: >-\n  {first}\n\n  {second}",
            "literal_blank_line": "description: |-\n  {first}\n\n  {second}",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = write_plugin(Path(tmp))
            for form, template in templates.items():
                with self.subTest(form=form):
                    # The same multiline forms at the cap remain valid controls.
                    metadata = template.format(first="é" * 149, second="é" * 150) + "\nname: alpha"
                    path = write_skill(root, "alpha", "description: BODY_ONLY", metadata)
                    self.assertEqual(description(path.read_text()), "é" * 149 + " " + "é" * 150)
                    assert_skill_descriptions(self, root)
                    metadata = template.format(first="é" * 199, second="é" * 200) + "\nname: alpha"
                    path = write_skill(root, "alpha", "description: BODY_ONLY", metadata)
                    value = description(path.read_text())
                    self.assertEqual(len(value), 400)
                    self.assertEqual(value, expected, "do not count the following key or body")
                    with self.assertRaisesRegex(AssertionError, "alpha description exceeds 300"):
                        assert_skill_descriptions(self, root)

    def test_pointer_controls_require_a_skill_file_not_just_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_plugin(Path(tmp))
            with self.assertRaisesRegex(AssertionError, "missing skill alpha"):
                assert_skill_pointers(self, root)
            (root / "skills" / "alpha").mkdir(parents=True)
            with self.assertRaisesRegex(AssertionError, "missing skill alpha"):
                assert_skill_pointers(self, root)
            write_skill(root, "alpha", "Generic rule.")
            assert_skill_pointers(self, root)

    def test_rendered_budget_is_independent_of_on_demand_skill_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_plugin(Path(tmp))
            before = rendered_sizes(1, root)
            body = "## Generic detail\n\n" + "é" * 500
            write_skill(root, "alpha", body, "description: Use for generic detail.")
            after = rendered_sizes(1, root)
            self.assertEqual(after, rendered_sizes(40, root), "prompt bytes must be path independent")
            for runtime in runtimes.NAMES:
                with self.subTest(runtime=runtime):
                    self.assertEqual(after[runtime], before[runtime],
                                     "on-demand skill bodies must not consume starting prompt bytes")

    def test_non_claude_prompts_do_not_keep_claude_only_phrases(self):
        # start_coordinator.prompt() swaps these by exact-string .replace(); an edit to the agent
        # file that breaks the match would otherwise ship the Claude tool names to other hosts.
        with tempfile.TemporaryDirectory() as tmp:
            for runtime in ("codex", "cursor", "agy"):
                prompt = start.prompt(runtime, ROOT, Path(tmp))
                for phrase in ("CronCreate", "list sessions;"):
                    with self.subTest(runtime=runtime, phrase=phrase):
                        self.assertNotIn(phrase, prompt)


if __name__ == "__main__":
    unittest.main()
