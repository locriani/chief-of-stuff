"""Unit tests for scripts/dispatch_prompt.py: the assignment a spawned session wakes up holding.

The prompt is derived, never authored. Nothing reaches a spawned session that is not already in the
tracker the user approved, which is why every field here is read back off disk rather than passed
in. The one variable argument is the lane name, and a lane name that is not a row is refused — so a
shell expansion that reached this far produces a refusal rather than a payload.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import dispatch_prompt as dp  # noqa: E402

CLAUDE = """# Workspace

## Coordinator

- User: Robin
- Daily log dir: `daily/`
- Tracker: `daily/<date>-tracker.md`
- Worktrees: `trees/`
- Timezone: America/Chicago
"""

TRACKER = """# Tracker 2026-09-18

## Lanes

| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
| Security audit | unassigned | open | 09:00 |  | Checklist: Security audit of the upload handler |
| Draft release notes | Robin | open | 09:00 |  | Checklist: Draft release notes |

## File ownership

- Security audit: `src/a/` (findings go to `notes/audit.md`)

## Log

- 09:00 opened the day
"""


def workspace(tracker: str = TRACKER, claude_md: str = CLAUDE):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "CLAUDE.md").write_text(claude_md)
    (root / "daily").mkdir()
    (root / "daily" / "2026-09-18-tracker.md").write_text(tracker)
    return tmp, root


class ComposeTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root = workspace()
        self.addCleanup(self.tmp.cleanup)

    def test_it_names_the_lane_as_the_tracker_spells_it(self):
        body = dp.compose(self.root, "2026-09-18", "Security audit")
        self.assertIn("Lane: Security audit", body)

    def test_it_names_the_tracker_path_from_the_coordinator_block(self):
        body = dp.compose(self.root, "2026-09-18", "Security audit")
        self.assertIn("Tracker: daily/2026-09-18-tracker.md", body)

    def test_a_lane_that_is_not_a_row_is_refused(self):
        """The one variable argument, and the reason an expansion cannot become a payload."""
        for absent in ("Upload handler fix", "$(whoami)", "`id`", ""):
            with self.assertRaises(dp.RefusedError, msg=absent):
                dp.compose(self.root, "2026-09-18", absent)

    def test_the_refusal_says_what_it_looked_for(self):
        with self.assertRaises(dp.RefusedError) as e:
            dp.compose(self.root, "2026-09-18", "Nonexistent lane")
        self.assertIn("Nonexistent lane", str(e.exception))
        self.assertIn("2026-09-18-tracker.md", str(e.exception))

    def test_a_missing_tracker_is_refused_rather_than_composed_around(self):
        with self.assertRaises(dp.RefusedError):
            dp.compose(self.root, "2026-09-19", "Security audit")


if __name__ == "__main__":
    unittest.main()
