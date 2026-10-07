"""A byte budget for agents/chief-of-stuff.md and the prompts rendered from it (#407).

The agent file is loaded whole at every session start, so its size is a recurring cost. These are
ceilings, not targets.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import runtimes  # noqa: E402
import start_coordinator as start  # noqa: E402
from evals.test_dispatch_prompt import one_shot_paragraph, sections  # noqa: E402

# Each later slice lowers these in a red commit before it trims the text.
TOTAL = 82_800            # whole agent file, bytes
MAX_LINE = 8_400          # longest single line
ONE_SHOT_PARAGRAPH = 4_400  # kept: slice 2 trims this paragraph
PROMPT = {"claude": 82_600, "codex": 75_000, "cursor": 75_100, "agy": 75_200}  # rendered, path bytes removed
CEILING = {  # `## ` section -> bytes, heading line included
    "Role": 1600, "Dispatch authority": 1100, "Browser safety": 410, "Writing": 780,
    "Config": 1400, "Clock": 1850, "Calendar": 910, "Open the day": 2000, "Resume": 4830,
    "Write authority": 2310, "Filing": 1360, "Human-only actions": 1280, "Dispatch": 15210,
    "Assign": 2560, "Brief": 730, "Pipeline": 6150, "Triage": 3770, "Check": 2780,
    "Sessions": 7380, "Relay": 2510, "Tracker": 10930, "Notices": 740, "Board": 1750,
    "Requirements": 1580, "Share": 460, "Asks": 5740, "Notify": 1940,
}


def rendered_sizes(pad: int) -> dict:
    """Each host's prompt size with the release and workspace paths taken out, so a short CI path
    and a long checkout path budget the same. `pad` lengthens both paths."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / ("w" * pad) / "workspace"
        root.mkdir(parents=True)
        release = start.install(ROOT, base / ("i" * pad))
        sizes = {}
        for runtime in runtimes.NAMES:
            text = start.prompt(runtime, release, root).replace(str(release), "").replace(str(root), "")
            sizes[runtime] = len(text.encode())
        return sizes


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
        self.assertLessEqual(len(one_shot_paragraph(self.text).encode()), ONE_SHOT_PARAGRAPH)

    def test_every_hosts_rendered_prompt_is_capped_whatever_the_path_length(self):
        short, long = rendered_sizes(1), rendered_sizes(40)
        self.assertEqual(short, long)
        self.assertEqual(set(PROMPT), set(short))
        for runtime, size in short.items():
            with self.subTest(runtime=runtime):
                self.assertLessEqual(size, PROMPT[runtime])

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
