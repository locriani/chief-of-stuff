"""#57: one rule for a read-only task, read the same way by the prompt and the launcher."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ownership  # noqa: E402


class ReadOnlyTest(unittest.TestCase):
    def read_only(self, paths: str) -> bool:
        # getattr: until the rule exists the failure is an assertion, not an AttributeError.
        return getattr(ownership, "read_only", lambda _: None)(paths)

    def test_a_cell_that_starts_with_the_word_none_is_read_only(self):
        for cell in ("none", "None; read-only review", "  NONE; read-only", "none; worktree `t` (b)"):
            with self.subTest(cell=cell):
                self.assertIs(self.read_only(cell), True)

    def test_a_cell_that_only_starts_with_the_letters_is_not(self):
        for cell in ("nonexistent/dir/", "`src/a/`; none of it generated", ""):
            with self.subTest(cell=cell):
                self.assertIs(self.read_only(cell), False)


if __name__ == "__main__":
    unittest.main()
