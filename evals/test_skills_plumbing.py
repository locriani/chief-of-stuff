"""Pinned skill files and on-demand host prompts (#511), with generic temporary fixtures."""

import hashlib
import re
import tempfile
import unittest
from pathlib import Path

import start_coordinator as start
from evals.rules_text import skill_body
from evals.skill_fixtures import AGENT, write_plugin, write_skill

NON_CLAUDE = ("codex", "cursor", "agy")
# No-skills baseline from AGENT in skill_fixtures, refreshed for main's Check adaptation.
# Only temporary paths are normalized; whitespace, substitutions, adapter text and START
# all remain byte-sensitive.
LEGACY_SHA256 = {
    "claude": "e03c9850cccd6c80db8e038cf10672f1c54afc1c48945098d958e958d6b1000d",
    "codex": "92e8307a069a35a4ca56e13b95acc0ff76749dfef69e7aca9b64971eaa39dc2a",
    "cursor": "a538a064eb92e4143f569e3f418e361c2d2a8281f80909ef7043a2f83ed32298",
    "agy": "480c12e14f76f349005afc8905106d89d0868862d8ef6dbc512caa17949bec55",
}


class SkillsPlumbingTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.plugin = write_plugin(self.base / "plugin")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()

    def skill_tree(self):
        write_skill(self.plugin, "alpha", "## Generic rule\n\nDo the generic task.\n", "description: Use for a generic task.")
        write_skill(self.plugin, "zeta", "## Other rule\n\nDo the other task.\n")
        # Every supporting file matters, including nested, hidden and binary assets.
        for name, data in {"alpha/references/detail.md": b"generic reference\n",
                           "alpha/assets/icon.bin": b"\x00\xff\x80",
                           "zeta/.note": b"generic hidden support\n"}.items():
            path = self.plugin / "skills" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return {p.relative_to(self.plugin): p.read_bytes()
                for p in (self.plugin / "skills").rglob("*") if p.is_file()}

    def test_files_and_install_preserve_every_file_in_the_skills_tree(self):
        expected = self.skill_tree()
        selected = {p.relative_to(self.plugin) for p in start.files(self.plugin)}
        release = start.install(self.plugin, self.base / "install")
        with self.subTest(operation="files"):
            self.assertTrue(set(expected) <= selected, f"skills missing from files(): {set(expected) - selected}")
        with self.subTest(operation="install"):
            installed = {p.relative_to(release): p.read_bytes()
                         for p in (release / "skills").rglob("*") if p.is_file()}
            self.assertEqual(installed, expected, "release must copy the whole skills tree byte for byte")
        for folder in ("agents", "scripts", "assets"):
            self.assertTrue((release / folder).is_dir(), folder)

    def test_a_support_file_change_creates_a_new_pinned_release(self):
        self.skill_tree()
        first = start.install(self.plugin, self.base / "install")
        reference = self.plugin / "skills" / "alpha" / "references" / "detail.md"
        reference.write_text("updated generic reference\n")
        second = start.install(self.plugin, self.base / "install")
        self.assertNotEqual(first, second, "skill support files must participate in the release digest")
        self.assertEqual((first / reference.relative_to(self.plugin)).read_bytes(), b"generic reference\n")
        self.assertEqual((second / reference.relative_to(self.plugin)).read_bytes(), reference.read_bytes())

    def test_install_without_a_skills_directory_keeps_working(self):
        release = start.install(self.plugin, self.base / "install")
        self.assertEqual(release, start.install(self.plugin, self.base / "install"))
        for name in ("agents/chief-of-stuff.md", "scripts/tool.py", "assets/example.txt"):
            self.assertEqual((release / name).read_bytes(), (self.plugin / name).read_bytes())
        self.assertFalse((release / "skills").exists())

    def test_non_claude_prompts_exclude_fixture_skill_text_and_named_headings(self):
        bodies = {
            "alpha": "\n \tALPHA_ON_DEMAND_ONLY_511\t \n\n",
            "zeta": ("## Check\n\nZETA_ON_DEMAND_ONLY_511: ${CLAUDE_PLUGIN_ROOT}/scripts/tool.py\n\n"
                     "## Sessions\n\nSKILL_SESSIONS\n\n## Relay\n\nSKILL_RELAY"),
        }
        # The path name identifies the skill; metadata and support files are not prompt text either.
        write_skill(self.plugin, "zeta", bodies["zeta"])
        write_skill(self.plugin, "alpha", bodies["alpha"],
                    "description: FRONTMATTER_ONLY\nname: not-the-path")
        (self.plugin / "skills" / "alpha" / "README.md").write_text("SUPPORT_ONLY")
        release = start.install(self.plugin, self.base / "install")
        for runtime in NON_CLAUDE:
            prompt = start.prompt(runtime, release, self.workspace)
            for name, marker in (("alpha", "ALPHA_ON_DEMAND_ONLY_511"),
                                 ("zeta", "ZETA_ON_DEMAND_ONLY_511")):
                with self.subTest(runtime=runtime, skill=name, check="body"):
                    self.assertFalse(marker in prompt, f"{runtime} starts with the {name} skill body")
                with self.subTest(runtime=runtime, skill=name, check="heading"):
                    self.assertFalse(f"## {name}" in prompt.splitlines(),
                                     f"{runtime} starts with a named {name} skill section")
            for excluded in ("FRONTMATTER_ONLY", "name: not-the-path", "SUPPORT_ONLY"):
                with self.subTest(runtime=runtime, excluded=excluded):
                    self.assertNotIn(excluded, prompt)

    def assert_pinned_skill_pointers(self, runtime, release, expected_names):
        agent = (release / "agents" / "chief-of-stuff.md").read_text()
        pointer_lines = [line for line in agent.splitlines()
                         if "${CLAUDE_PLUGIN_ROOT}/skills/" in line]
        names = re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/skills/([^/\s`]+)/SKILL\.md", agent)
        self.assertEqual(set(names), expected_names, "exercise every expected skill pointer")
        self.assertTrue(pointer_lines, "the fixture must include an on-demand pointer")
        prompt = start.prompt(runtime, release, self.workspace)
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", prompt)
        for line in pointer_lines:
            with self.subTest(runtime=runtime, pointer=line):
                self.assertIn(line.replace("${CLAUDE_PLUGIN_ROOT}", str(release)), prompt.splitlines(),
                              "preserve the whole pointer line with its pinned release path")
        for name in names:
            with self.subTest(runtime=runtime, skill=name):
                self.assertTrue((release / "skills" / name / "SKILL.md").is_file(),
                                "the host must be able to read the named file from the release")

    def test_non_claude_fixture_pointers_resolve_to_installed_skill_files(self):
        agent = AGENT.replace(
            "Run python3 ${CLAUDE_PLUGIN_ROOT}/scripts/tool.py.",
            "Before another generic task, Read ${CLAUDE_PLUGIN_ROOT}/skills/zeta/SKILL.md.\n"
            "Run python3 ${CLAUDE_PLUGIN_ROOT}/scripts/tool.py.")
        write_plugin(self.plugin, agent)
        write_skill(self.plugin, "alpha", "ALPHA_ON_DEMAND_ONLY_511")
        write_skill(self.plugin, "zeta", "ZETA_ON_DEMAND_ONLY_511")
        release = start.install(self.plugin, self.base / "install")
        self.assertNotEqual(release, self.plugin)
        for runtime in NON_CLAUDE:
            with self.subTest(runtime=runtime):
                self.assert_pinned_skill_pointers(runtime, release, {"alpha", "zeta"})

    def test_non_claude_host_section_replacement_preserves_other_agent_rules(self):
        write_skill(self.plugin, "alpha", "ON_DEMAND_ONLY_511")
        for runtime in NON_CLAUDE:
            with self.subTest(runtime=runtime):
                prompt = start.prompt(runtime, self.plugin, self.workspace)
                self.assertNotIn("CLAUDE_CHECK_ONLY", prompt)
                check = prompt.split("## Check\n", 1)[1].split("## Sessions\n", 1)[0]
                self.assertIn("\n\nThe standing waiting-on list stays generic.\n\n", check)
                self.assertNotIn("CLAUDE_SESSIONS_ONLY", prompt)
                self.assertIn("RELAY_KEPT", prompt)

    def test_claude_keeps_on_demand_skills_out_of_its_prompt(self):
        before = start.prompt("claude", self.plugin, self.workspace)
        write_skill(self.plugin, "alpha", "ALPHA_ON_DEMAND_ONLY_511", "description: A generic task.")
        write_skill(self.plugin, "zeta", "ZETA_ON_DEMAND_ONLY_511")
        self.assertEqual(start.prompt("claude", self.plugin, self.workspace), before)
        # prompt() already substitutes this variable for Claude; preserve that behavior.
        self.assertIn(str(self.plugin / "skills" / "alpha" / "SKILL.md"), before)
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", before)

    def test_shipped_skill_bodies_and_named_headings_are_absent_from_host_prompts(self):
        root = Path(__file__).resolve().parent.parent
        markers = {
            "decision-page": "Never publish a decision page as an artifact, and never open it.",
            "optional-features": "Notifications are off by default, including an empty `[notify]` table.",
            "tracker-rows": "Never set the cell by hand or write that line as free text.",
        }
        release = start.install(root, self.base / "install")
        agent = (release / "agents" / "chief-of-stuff.md").read_text()
        for name, marker in markers.items():
            path = release / "skills" / name / "SKILL.md"
            self.assertTrue(path.is_file(), f"{name} skill is missing")
            self.assertIn(marker, skill_body(path.read_text()), "marker must identify shipped skill prose")
            self.assertNotIn(marker, agent, "marker must distinguish the skill from agent rules")
        for runtime in (*NON_CLAUDE, "claude"):
            prompt = start.prompt(runtime, release, self.workspace)
            for name, marker in markers.items():
                with self.subTest(runtime=runtime, skill=name, check="body"):
                    self.assertFalse(marker in prompt, f"{runtime} starts with the {name} skill body")
                with self.subTest(runtime=runtime, skill=name, check="heading"):
                    self.assertFalse(f"## {name}" in prompt.splitlines(),
                                     f"{runtime} starts with a named {name} skill section")

    def test_shipped_skill_pointer_lines_resolve_to_installed_files_on_every_host(self):
        root = Path(__file__).resolve().parent.parent
        release = start.install(root, self.base / "install")
        for runtime in (*NON_CLAUDE, "claude"):
            with self.subTest(runtime=runtime):
                self.assert_pinned_skill_pointers(runtime, release,
                                                  {"decision-page", "optional-features", "tracker-rows"})

    def test_no_skills_prompt_is_byte_identical_to_the_legacy_prompt(self):
        for empty_dir in (False, True):
            if empty_dir:
                (self.plugin / "skills").mkdir()
            for runtime, digest in LEGACY_SHA256.items():
                with self.subTest(runtime=runtime, empty_dir=empty_dir):
                    prompt = start.prompt(runtime, self.plugin, self.workspace)
                    normalized = prompt.replace(str(self.plugin), "<release>").replace(str(self.workspace), "<workspace>")
                    self.assertEqual(hashlib.sha256(normalized.encode()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
