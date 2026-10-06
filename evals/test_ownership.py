"""#57: one rule for a read-only task, read the same way by the prompt and the launcher."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ownership  # noqa: E402


class ReadOnlyTest(unittest.TestCase):
    def test_a_cell_that_starts_with_the_word_none_is_read_only(self):
        for cell in ("none", "None; read-only review", "  NONE; read-only", "none; worktree `t` (b)",
                     "none (read-only)", "none, review only"):
            with self.subTest(cell=cell):
                self.assertIs(ownership.read_only(cell), True)

    def test_a_cell_that_names_a_path_is_not_read_only_however_it_starts(self):
        for cell in ("none.py", "None/x.py", "none-such/ and src/a/", "nonexistent/dir/",
                     "src/a/; none of it generated", ""):
            with self.subTest(cell=cell):
                self.assertIs(ownership.read_only(cell), False)


if __name__ == "__main__":
    unittest.main()
