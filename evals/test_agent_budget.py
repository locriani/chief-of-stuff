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
# Slice 3 (#486): move Filing 1,324, Requirements 1,549 and Notify 1,896 bytes
# into optional-features, retaining the headings and three pinned pointer lines.
# rendered_sizes(1): agent 81,231; Claude 81,005, Codex 75,666, Cursor 75,774,
# AGY 75,830 (release/workspace paths removed). A verbatim move with ### skill
# headings projects agent 76,884; Claude 76,593, Codex 76,049, Cursor 76,157,
# AGY 76,213. Non-Claude ceilings still fit and stay unchanged (151/143/187 spare).
TOTAL = 77_700            # whole agent file, bytes
MAX_LINE = 8_400          # longest single line
ONE_SHOT_PARAGRAPH = 4_400  # kept: a later slice trims this paragraph
# Other hosts still append skill bodies, plus the new always-loaded rules.
PROMPT = {"claude": 77_500, "codex": 76_200, "cursor": 76_300, "agy": 76_400}  # rendered, path bytes removed
CEILING = {  # `## ` section -> bytes, heading line included
    "Role": 1600, "Dispatch authority": 1100, "Browser safety": 410, "Writing": 780,
    "Config": 1400, "Clock": 1850, "Calendar": 910, "Open the day": 2000, "Resume": 4830,
    "Write authority": 2310, "Filing": 360, "Human-only actions": 1280, "Dispatch": 15210,
    "Assign": 2560, "Brief": 730, "Pipeline": 6150, "Triage": 3770, "Check": 2780,
    "Sessions": 7380, "Relay": 2510, "Tracker": 10930, "Notices": 740, "Board": 1750,
    "Requirements": 360, "Share": 460, "Asks": 4700, "Notify": 360,
}
SKILL_CEILING = {"decision-page": 2_700, "optional-features": 5_200}  # moved bytes + metadata/organization/headroom
SKILLS_TOTAL = 7_900  # all SKILL.md files, bytes
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

    def test_rendered_budget_counts_appended_skill_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_plugin(Path(tmp))
            before = rendered_sizes(1, root)
            body = "## Generic detail\n\n" + "é" * 500
            write_skill(root, "alpha", body, "description: Use for generic detail.")
            after = rendered_sizes(1, root)
            self.assertEqual(after, rendered_sizes(40, root), "skill bytes must also be path independent")
            self.assertEqual(after["claude"], before["claude"])
            for runtime in ("codex", "cursor", "agy"):
                with self.subTest(runtime=runtime):
                    # A cap one byte below the body must be crossed by the full rendered prompt.
                    fixture_cap = before[runtime] + len(body.encode()) - 1
                    self.assertGreater(after[runtime], fixture_cap,
                                       "rendered prompt budget must count the appended skill body")

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
