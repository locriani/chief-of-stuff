"""Slice 1 of #484: release and host prompt plumbing, using only temporary plugin roots."""

import hashlib
import tempfile
import unittest
from pathlib import Path

import start_coordinator as start
from evals.skill_fixtures import write_plugin, write_skill

NON_CLAUDE = ("codex", "cursor", "agy")
# Frozen before skills support, from AGENT in skill_fixtures. Only the temporary paths are
# normalized: whitespace, substitutions, adapter text and START all remain byte-sensitive.
LEGACY_SHA256 = {
    "claude": "d656baeb0255a848acd914f65dae3e39a3f2ec48a2475bb8617848d471b91bf3",
    "codex": "b988c2fb6be5f3d8d3b1c3c1a596f76219d0811e27e056bec952add0ca63e705",
    "cursor": "98135264e33ef0ebb149106d331be921197f05d3d16f31f7666d45b86baaf0d5",
    "agy": "7a085e63b6cd376c5b5383e442801825499fa677001658063e5a301a6a9652be",
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

    def test_non_claude_hosts_append_all_bodies_in_sorted_order_under_named_headings(self):
        # Deliberately misleading frontmatter name: the skill's path name identifies its pointer.
        zeta_body = "ZETA_BODY\n\n---\n\nhosts: claude\nZETA_AFTER_BREAK"
        # A hosts key in body/example text is not a frontmatter host restriction.
        write_skill(self.plugin, "zeta", zeta_body)
        write_skill(self.plugin, "alpha", "ALPHA_BODY", "description: FRONTMATTER_ONLY\nname: not-the-path")
        (self.plugin / "skills" / "alpha" / "README.md").write_text("SUPPORT_ONLY")
        for runtime in NON_CLAUDE:
            with self.subTest(runtime=runtime):
                prompt = start.prompt(runtime, self.plugin, self.workspace)
                headings = []
                for name, body in (("alpha", "ALPHA_BODY"), ("zeta", zeta_body)):
                    self.assertEqual(prompt.count(body), 1, f"{runtime} must append the {name} body once")
                    heading = next((line for line in prompt.splitlines()
                                    if line.startswith("#") and name in line and "SKILL.md" not in line), None)
                    self.assertIsNotNone(heading, f"{name} needs a skill-naming heading")
                    self.assertLess(prompt.index(heading), prompt.index(body))
                    headings.append(prompt.index(heading))
                self.assertLess(headings[0], headings[1], "skills must be sorted by path name")
                self.assertGreater(headings[0], prompt.index("RELAY_KEPT"), "skills follow the agent rules")
                self.assertNotIn("FRONTMATTER_ONLY", prompt)
                self.assertNotIn("name: not-the-path", prompt)
                self.assertNotIn("SUPPORT_ONLY", prompt)

    def test_appended_bodies_rewrite_plugin_root_and_survive_host_section_replacement(self):
        body = ("## Check\n\nSKILL_CHECK: ${CLAUDE_PLUGIN_ROOT}/scripts/tool.py\n\n"
                "## Sessions\n\nSKILL_SESSIONS\n\n## Relay\n\nSKILL_RELAY")
        write_skill(self.plugin, "alpha", body, "description: Use for a generic check.")
        for runtime in NON_CLAUDE:
            with self.subTest(runtime=runtime):
                prompt = start.prompt(runtime, self.plugin, self.workspace)
                self.assertIn(body.replace("${CLAUDE_PLUGIN_ROOT}", str(self.plugin)), prompt,
                              "host adapter regexes must not swallow or rewrite appended skill sections")
                self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", prompt)
                self.assertIn(str(self.plugin / "skills" / "alpha" / "SKILL.md"), prompt)
                self.assertNotIn("CLAUDE_CHECK_ONLY", prompt)
                self.assertNotIn("CLAUDE_SESSIONS_ONLY", prompt)
                self.assertIn("RELAY_KEPT", prompt)

    def test_claude_only_skills_do_not_change_non_claude_prompts(self):
        write_skill(self.plugin, "alpha", "PUBLIC_BODY", "description: Use for a generic task.")
        before = {runtime: start.prompt(runtime, self.plugin, self.workspace) for runtime in NON_CLAUDE}
        write_skill(self.plugin, "private", "CLAUDE_PRIVATE_BODY", "description: Use for Claude tasks.\nhosts: claude")
        for runtime in NON_CLAUDE:
            with self.subTest(runtime=runtime):
                self.assertEqual(start.prompt(runtime, self.plugin, self.workspace), before[runtime])

    def test_claude_keeps_on_demand_skills_out_of_its_prompt(self):
        before = start.prompt("claude", self.plugin, self.workspace)
        write_skill(self.plugin, "alpha", "PUBLIC_BODY", "description: Use for a generic task.")
        write_skill(self.plugin, "private", "CLAUDE_PRIVATE_BODY", "hosts: claude")
        self.assertEqual(start.prompt("claude", self.plugin, self.workspace), before)
        self.assertIn(str(self.plugin / "skills" / "alpha" / "SKILL.md"), before)
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", before)

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
