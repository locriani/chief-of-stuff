"""Unit tests for scripts/tree_state.py: what a worktree holds that exists nowhere else (#43).

No git here. `TreeState` is the four numbers a reader takes from git; `at_risk` is the rule over them.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from tree_state import TreeState, at_risk  # noqa: E402


def state(dirty: int = 0, off_origin: int | None = 0, unpushed: int | None = None) -> TreeState:
    return TreeState(branch="feat/x", dirty=dirty, off_origin=off_origin, unpushed=unpushed)


class AtRiskTest(unittest.TestCase):
    def test_a_clean_tree_on_origin_main_holds_nothing(self) -> None:
        self.assertEqual(at_risk(state()), ())

    def test_uncommitted_files_are_counted(self) -> None:
        self.assertEqual(at_risk(state(dirty=2)), ("2 uncommitted",))

    def test_a_commit_with_no_upstream_exists_only_here(self) -> None:
        """The field report: one local commit, never pushed, and the audit printed nothing."""
        self.assertEqual(at_risk(state(off_origin=1)), ("1 not on origin/main", "no upstream"))

    def test_a_commit_ahead_of_its_upstream_is_unpushed(self) -> None:
        self.assertEqual(at_risk(state(off_origin=3, unpushed=1)), ("3 not on origin/main", "1 unpushed"))

    def test_a_branch_pushed_to_its_upstream_is_safe_off_main(self) -> None:
        """Unmerged is not at risk once a remote holds it: it is recoverable by name from there."""
        self.assertEqual(at_risk(state(off_origin=3, unpushed=0)), ())

    def test_a_branch_at_main_needs_no_upstream(self) -> None:
        self.assertEqual(at_risk(state(off_origin=0, unpushed=None)), ())

    def test_an_unknown_origin_is_never_read_as_safe(self) -> None:
        self.assertEqual(at_risk(state(off_origin=None)), ("origin/main unknown", "no upstream"))

    def test_but_a_pushed_branch_is_safe_whatever_origin_main_says(self) -> None:
        self.assertEqual(at_risk(state(off_origin=None, unpushed=0)), ())

    def test_dirt_and_commits_are_both_named(self) -> None:
        self.assertEqual(at_risk(state(dirty=1, off_origin=1)), ("1 uncommitted", "1 not on origin/main", "no upstream"))


if __name__ == "__main__":
    unittest.main()
