"""Rule-text lints can follow prose into skills without losing their parsers (#484)."""

import tempfile
import unittest
from pathlib import Path

from evals.rules_text import one_shot_paragraph, rules_text, sections
from evals.skill_fixtures import write_plugin, write_skill


class RulesTextTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.plugin = write_plugin(Path(tmp.name) / "plugin", "## Role\n\nGeneric coordinator.\n")

    def test_no_skills_returns_the_agent_text_unchanged(self):
        self.assertEqual(rules_text(self.plugin), "## Role\n\nGeneric coordinator.\n")
        (self.plugin / "skills").mkdir()
        self.assertEqual(rules_text(self.plugin), "## Role\n\nGeneric coordinator.\n")

    def test_all_skill_bodies_follow_the_agent_in_sorted_order_without_frontmatter(self):
        # Creation order differs from path order; Claude-only rules still belong in eval lints.
        write_skill(self.plugin, "zeta", "## Last\n\nLast rule.\n", "description: Secret metadata.\nhosts: claude")
        write_skill(self.plugin, "alpha", "## First\n\nFirst rule.\n", "description: Other metadata.")
        self.assertEqual(rules_text(self.plugin),
                         "## Role\n\nGeneric coordinator.\n\n\n## First\n\nFirst rule.\n\n## Last\n\nLast rule.")

    def test_body_without_frontmatter_keeps_fences_and_thematic_breaks(self):
        body = "## Detail\n\nFirst.\n\n---\n\n```\n## Example\n```\n\n---\n\nLast."
        write_skill(self.plugin, "plain", body)
        write_skill(self.plugin, "yaml", body.replace("Detail", "More"), "description: Not a rule.")
        text = rules_text(self.plugin)
        self.assertIn(body, text)
        self.assertIn(body.replace("Detail", "More"), text)
        self.assertNotIn("Not a rule.", text)
        self.assertEqual(set(sections(text)), {"Role", "Detail", "More"})

    def test_sections_and_one_shot_paragraph_find_rules_after_a_move(self):
        paragraph = 'If `[workers] mode = "one-shot"`, launch the generic task.'
        write_skill(self.plugin, "dispatch", "## Dispatch\n\n" + paragraph + "\n\n```\n## Dispatch\n```\n",
                    "description: Use for generic dispatch.")
        text = rules_text(self.plugin)
        self.assertEqual(one_shot_paragraph(text), paragraph)
        self.assertEqual(set(sections(text)), {"Role", "Dispatch"})
        self.assertIn("```\n## Dispatch\n```", sections(text)["Dispatch"])
        with self.assertRaisesRegex(ValueError, "repeated heading: ## Dispatch"):
            sections(text + "\n## Dispatch\nDuplicate.")

    def test_supporting_files_are_not_rule_text(self):
        write_skill(self.plugin, "generic", "## Detail\n\nThe actual rule.")
        (self.plugin / "skills" / "generic" / "README.md").write_text("NOT_A_RULE")
        self.assertNotIn("NOT_A_RULE", rules_text(self.plugin))


if __name__ == "__main__":
    unittest.main()
