"""Unit tests for scripts/forge_review.py: what a run loses on a pull or merge request (#64)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import forge_review  # noqa: E402


def a_request(**overrides):
    """An armed, approved, mergeable request on head0."""
    fields = dict(number=58, title="Fix the handler", url="https://github.com/team/repo/pull/58",
                  head="head0", approval="approved", pipeline="passed", mergeable="yes", approver="Robin",
                  auto_merge=True)
    fields.update(overrides)
    return forge_review.Request(**fields)


class LostOnRunTest(unittest.TestCase):
    """`lost_on_run(before, after)` names what a run dropped (#64). Pure, and only about what the
    launcher can see across a run: the armed auto-merge and the approver's approval."""

    def test_a_cleared_auto_merge_is_lost(self):
        self.assertEqual(forge_review.lost_on_run(a_request(), a_request(auto_merge=False)), ["auto-merge"])

    def test_an_approval_that_does_not_survive_the_run_is_lost(self):
        self.assertEqual(forge_review.lost_on_run(a_request(), a_request(approval="none")), ["Robin's approval"])

    def test_an_approval_that_went_stale_on_a_new_head_is_lost(self):
        self.assertEqual(forge_review.lost_on_run(a_request(), a_request(head="head1", approval="stale")),
                         ["Robin's approval"])

    def test_nothing_is_lost_when_the_state_stands(self):
        self.assertEqual(forge_review.lost_on_run(a_request(), a_request(head="head1")), [])

    def test_an_unarmed_auto_merge_is_never_lost(self):
        self.assertEqual(forge_review.lost_on_run(a_request(auto_merge=False), a_request()), [])


if __name__ == "__main__":
    unittest.main()
