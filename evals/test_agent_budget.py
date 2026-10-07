"""A byte budget for agents/chief-of-stuff.md and the prompt rendered from it (#407).

The agent file is loaded whole at every session start, so its size is a recurring cost. These are
ceilings, not targets: each later slice lowers them in a red commit before it trims the text.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import start_coordinator as start  # noqa: E402

# Each later slice lowers these in a red commit before it trims the text.
TOTAL = 82_400            # whole agent file, bytes
MAX_LINE = 8_400          # longest single line
ONE_SHOT_PARAGRAPH = 4_400
CODEX_PROMPT = 75_000     # start_coordinator.prompt("codex"), bytes
CEILING = {  # `## ` section -> bytes, heading line included
    "Role": 1600, "Dispatch authority": 1100, "Browser safety": 410, "Writing": 780,
    "Config": 1400, "Clock": 1850, "Calendar": 910, "Open the day": 2000, "Resume": 4830,
    "Write authority": 2310, "Filing": 1360, "Human-only actions": 1280, "Dispatch": 15210,
    "Assign": 2560, "Brief": 730, "Pipeline": 5910, "Triage": 3770, "Check": 2670,
    "Sessions": 7380, "Relay": 2510, "Tracker": 10930, "Notices": 740, "Board": 1750,
    "Requirements": 1580, "Share": 460, "Asks": 5740, "Notify": 1940,
}


def sections(text: str) -> dict:
    """Bytes per `## ` section; a `## ` line inside a code fence is content, not a heading."""
    sizes, current, fenced = {}, None, False
    for line in text.split("\n"):
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("## "):
            current = line[3:].strip()
            sizes[current] = 0
        if current:
            sizes[current] += len(line.encode()) + 1
    return sizes


class AgentBudgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / "agents" / "chief-of-stuff.md").read_text()

    def test_total(self):
        self.assertLessEqual(len(self.text.encode()), TOTAL)

    def test_each_section_is_within_its_ceiling(self):
        for name, size in sections(self.text).items():
            with self.subTest(section=name):
                self.assertIn(name, CEILING, f"new section needs a budget line: ## {name}")
                self.assertLessEqual(size, CEILING[name])

    def test_a_fenced_heading_is_not_a_section(self):
        self.assertEqual(sections("## A\nx\n```\n## B\n```\ny\n"), {"A": 23})
        # The Resume code fence holds a literal `## Resume` line; a naive parse restarts the count there.
        self.assertGreater(sections(self.text)["Resume"], 4000)

    def test_no_line_outgrows_the_cap(self):
        for line in self.text.split("\n"):
            self.assertLessEqual(len(line.encode()), MAX_LINE, line[:60])

    def test_the_one_shot_paragraph_is_capped(self):
        paragraph = next(line for line in self.text.split("\n") if line.startswith('If `[workers] mode = "one-shot"`'))
        self.assertLessEqual(len(paragraph.encode()), ONE_SHOT_PARAGRAPH)

    def _prompt(self, runtime):
        with tempfile.TemporaryDirectory() as tmp:
            return start.prompt(runtime, ROOT, Path(tmp))

    def test_the_rendered_codex_prompt_is_capped(self):
        self.assertLessEqual(len(self._prompt("codex").encode()), CODEX_PROMPT)

    def test_non_claude_prompts_do_not_keep_claude_only_phrases(self):
        # start_coordinator.prompt() swaps these by exact-string .replace(); an edit to the agent
        # file that breaks the match would otherwise ship the Claude tool names to other hosts.
        for runtime in ("codex", "cursor", "agy"):
            prompt = self._prompt(runtime)
            for phrase in ("CronCreate", "list sessions"):
                with self.subTest(runtime=runtime, phrase=phrase):
                    self.assertNotIn(phrase, prompt)


if __name__ == "__main__":
    unittest.main()
