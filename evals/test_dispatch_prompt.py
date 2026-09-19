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

| context | paths |
|---|---|
| Security audit | `src/a/`, `notes/audit.md` |

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
        self.assertIn(f"Tracker: {self.root.resolve()}/daily/2026-09-18-tracker.md", body)


    def test_the_tracker_path_resolves_from_the_worktree_not_the_workspace(self):
        """The file is read by a session whose cwd is its tree, not the root the path is written against.

        A relative tracker path sent the first real spawn hunting: ls, cat, then out of its own tree
        looking for a file that was never reachable from where it stood.
        """
        body = dp.compose(self.root, "2026-09-18", "Security audit")
        line = [x for x in body.splitlines() if x.startswith("Tracker: ")][0]
        named = Path(line.removeprefix("Tracker: "))
        self.assertTrue(named.is_absolute(), f"{named} does not resolve from a worktree")
        self.assertTrue(named.exists(), f"{named} is not there")

    def test_it_still_says_which_workspace_the_lane_lives_in(self):
        body = dp.compose(self.root, "2026-09-18", "Security audit")
        self.assertIn(f"Workspace: {self.root.resolve()}", body)

    def test_a_lane_that_is_not_a_row_is_refused(self):
        """The one variable argument, and the reason an expansion cannot become a payload."""
        for absent in ("Upload handler fix", "$(whoami)", "`id`", ""):
            with self.assertRaises(dp.RefusedError, msg=absent):
                dp.compose(self.root, "2026-09-18", absent)


    def test_a_lane_that_already_has_an_owner_is_refused(self):
        """Two trees were handed the same lane and the same file. The rule said unassigned; nothing checked.

        Found by the first dispatched session, which noticed a sibling worktree holding a
        byte-identical assignment and stopped rather than committing over it.
        """
        with self.assertRaises(dp.RefusedError) as e:
            dp.compose(self.root, "2026-09-18", "Draft release notes")
        self.assertIn("Robin", str(e.exception))

    def test_the_assignment_says_which_tree_it_was_written_for(self):
        """So a session can tell whether the dispatch it is reading was addressed to it."""
        body = dp.compose(self.root, "2026-09-18", "Security audit", worktree=Path("/tmp/trees/wt-audit"))
        self.assertIn("Worktree: /tmp/trees/wt-audit", body)

    def test_the_refusal_says_what_it_looked_for(self):
        with self.assertRaises(dp.RefusedError) as e:
            dp.compose(self.root, "2026-09-18", "Nonexistent lane")
        self.assertIn("Nonexistent lane", str(e.exception))
        self.assertIn("2026-09-18-tracker.md", str(e.exception))

    def test_a_missing_tracker_is_refused_rather_than_composed_around(self):
        with self.assertRaises(dp.RefusedError):
            dp.compose(self.root, "2026-09-19", "Security audit")


class AssignmentTest(unittest.TestCase):
    """The remaining lines. Two lines were enough for a session to find its way and not to act."""

    def setUp(self):
        self.tmp, self.root = workspace()
        self.addCleanup(self.tmp.cleanup)

    def body(self, lane="Security audit", **kw):
        return dp.compose(self.root, "2026-09-18", lane, **kw)

    def test_the_whole_item_cell_travels_as_the_ask(self):
        """The item is the ask. A session that reads only that row has to know what done looks like."""
        ask = "Security audit: the upload handler. Done when every path is checked and the findings are in notes/audit.md"
        tmp, root = workspace(TRACKER.replace("| Security audit | unassigned", f"| {ask} | unassigned"))
        self.addCleanup(tmp.cleanup)
        body = dp.compose(root, "2026-09-18", ask)
        self.assertIn("Done when every path is checked", body)

    def test_it_names_the_paths_the_lane_owns(self):
        self.assertIn("Owns: `src/a/`, `notes/audit.md`", self.body())

    def test_owning_nothing_is_said_rather_than_left_out(self):
        tmp, root = workspace(TRACKER.replace("| Security audit | `src/a/`, `notes/audit.md` |",
                                              "| Security audit | none |"))
        self.addCleanup(tmp.cleanup)
        self.assertIn("Owns: none", dp.compose(root, "2026-09-18", "Security audit"))

    def test_a_lane_with_no_file_ownership_row_is_refused(self):
        """Ownership is what keeps two sessions off one file. A dispatch without it is finding 55 again."""
        tmp, root = workspace(TRACKER.replace("| Security audit | `src/a/`, `notes/audit.md` |", ""))
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(dp.RefusedError) as e:
            dp.compose(root, "2026-09-18", "Security audit")
        self.assertIn("File ownership", str(e.exception))

    def test_the_ownership_row_may_be_keyed_on_the_short_name(self):
        """After the spawn the coordinator rewrites that cell to name the tree, so the long form goes."""
        long_item = "Security audit: the upload handler, every path, findings into notes/audit.md"
        tmp, root = workspace(TRACKER.replace("| Security audit | unassigned", f"| {long_item} | unassigned"))
        self.addCleanup(tmp.cleanup)
        self.assertIn("Owns: `src/a/`", dp.compose(root, "2026-09-18", long_item))

    def test_a_short_name_before_the_colon_is_still_a_key(self):
        """`short_name` only honours the head when it is 12 characters or more, and then ellipsises.

        Found by hand: a lane called `Tab check: ...` refused with a key ending in an ellipsis, which
        is a key no one could write into File ownership. A refusal has to name something writable.
        """
        item = "Tab check: confirm this session came up in a tab and change nothing at all"
        tmp, root = workspace(TRACKER.replace("| Security audit | unassigned", f"| {item} | unassigned")
                                     .replace("| Security audit | `src/a/`", "| Tab check | `src/a/`"))
        self.addCleanup(tmp.cleanup)
        self.assertIn("Owns: `src/a/`", dp.compose(root, "2026-09-18", item))

    def test_a_refusal_names_a_key_that_could_be_written(self):
        tmp, root = workspace(TRACKER.replace("| Security audit | `src/a/`, `notes/audit.md` |", ""))
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(dp.RefusedError) as e:
            dp.compose(root, "2026-09-18", "Security audit")
        self.assertNotIn("\u2026", str(e.exception))

    def test_it_says_to_commit_and_not_to_merge(self):
        """Superseded 0.12.0: this pinned `Write only: do not commit or push.`, which contradicted the
        workspace rule the assignment exists to carry — every session makes meaningful small commits,
        because uncommitted work is how work gets lost (Zach, 2026-09-18 23:00). The gate is the merge."""
        self.assertRegex(self.body(), r"(?m)^Commits: Commit small and often\b")
        self.assertIn("never merge", self.body())

    def test_it_says_what_to_reply_with(self):
        self.assertRegex(self.body(), r"(?m)^Report: \S")

    def test_the_requirement_the_lane_serves_travels_when_there_is_one(self):
        self.assertIn("Security audit of the upload handler", self.body())

    def test_a_blank_checklist_leaves_the_line_out_rather_than_writing_an_empty_one(self):
        tmp, root = workspace(TRACKER.replace("| Checklist: Security audit of the upload handler |", "|  |"))
        self.addCleanup(tmp.cleanup)
        self.assertNotRegex(dp.compose(root, "2026-09-18", "Security audit"), r"(?m)^Requirement:\s*$")


class HeaderTest(unittest.TestCase):
    """What the coordinator can neither forge nor omit, read hours in, after a compaction."""

    def setUp(self):
        self.tmp, self.root = workspace()
        self.addCleanup(self.tmp.cleanup)

    def test_it_says_the_file_is_not_authority(self):
        body = dp.compose(self.root, "2026-09-18", "Security audit")
        self.assertIn("not authority", body)

    def test_it_says_to_hand_back_a_lane_that_does_not_add_up(self):
        """Finding 56: the failure mode to design against is inventing plausible work, not stopping."""
        body = dp.compose(self.root, "2026-09-18", "Security audit")
        self.assertIn("hand it back", body)
        self.assertIn("assumption", body)

    def test_it_resolves_the_tension_between_staying_in_the_tree_and_reading_the_tracker(self):
        body = dp.compose(self.root, "2026-09-18", "Security audit")
        self.assertIn("Reading the tracker named below is expected", body)

    def test_it_tells_the_session_to_register(self):
        body = dp.compose(self.root, "2026-09-18", "Security audit")
        self.assertIn("Register first", body)
        self.assertIn("name [ref]", body)

    def test_it_names_the_coordinator_when_one_was_given(self):
        body = dp.compose(self.root, "2026-09-18", "Security audit", coordinator="gauntlet-d3")
        self.assertIn("gauntlet-d3", body)

    def test_it_still_says_to_register_when_no_coordinator_was_named(self):
        body = dp.compose(self.root, "2026-09-18", "Security audit")
        self.assertIn("chief-of-stuff coordinator", body)

    def test_a_coordinator_name_that_is_not_a_session_name_is_refused(self):
        for bad in ("$(whoami)", "a b; rm", "\x1b]0;x\x07", "x" * 200):
            with self.assertRaises(dp.RefusedError, msg=bad):
                dp.compose(self.root, "2026-09-18", "Security audit", coordinator=bad)

    def test_a_ref_may_ride_along_with_the_name(self):
        body = dp.compose(self.root, "2026-09-18", "Security audit", coordinator="gauntlet-d3 [0d6cf4]")
        self.assertIn("gauntlet-d3 [0d6cf4]", body)


class QuarantineTest(unittest.TestCase):
    """The tracker is not a trusted channel: a coordinator writes into it, and so does a peer."""

    def field(self, tracker):
        tmp, root = workspace(tracker)
        self.addCleanup(tmp.cleanup)
        return root

    def test_session_origin_text_in_a_composed_field_is_refused(self):
        """`(via <session>)` is what this system marks session-origin text with. It is data, not an instruction."""
        for spoiled in (
            TRACKER.replace("| Security audit | unassigned", "| Security audit (via 4821-audit) | unassigned"),
            TRACKER.replace("`src/a/`, `notes/audit.md`", "`src/a/` (via 4821-audit)"),
            TRACKER.replace("Checklist: Security audit", "Checklist: (via 4821-audit) Security audit"),
        ):
            root = self.field(spoiled)
            lane = "Security audit (via 4821-audit)" if "Security audit (via" in spoiled else "Security audit"
            with self.assertRaises(dp.RefusedError):
                dp.compose(root, "2026-09-18", lane)

    def test_a_control_character_is_refused(self):
        """An OSC payload reaching a terminal is a separate vector from prompt injection."""
        root = self.field(TRACKER.replace("`src/a/`", "`src/a/`\x1b]0;pwned\x07"))
        with self.assertRaises(dp.RefusedError) as e:
            dp.compose(root, "2026-09-18", "Security audit")
        self.assertIn("control character", str(e.exception))

    def test_a_line_longer_than_the_cap_is_refused(self):
        root = self.field(TRACKER.replace("`src/a/`", "`" + "a" * (dp.LINE_CAP + 1) + "`"))
        with self.assertRaises(dp.RefusedError):
            dp.compose(root, "2026-09-18", "Security audit")

    def test_a_real_tracker_row_is_well_inside_the_caps(self):
        """The caps are a sanity bound. Live item cells run to 1700 characters and must still compose."""
        self.assertGreater(dp.LINE_CAP, 1700)
        long_item = "Security audit: " + ("word " * 340).strip()
        root = self.field(TRACKER.replace("| Security audit | unassigned", f"| {long_item} | unassigned"))
        self.assertIn("Owns: `src/a/`", dp.compose(root, "2026-09-18", long_item))


if __name__ == "__main__":
    unittest.main()


class StopCostsSomethingTest(unittest.TestCase):
    """The assignment's own text is three-fifths prohibition. A session that must stop needs a price, not another no."""

    def setUp(self):
        self.tmp, self.root = workspace()
        self.addCleanup(self.tmp.cleanup)
        self.body = dp.compose(self.root, "2026-09-18", "Security audit")

    def test_the_assignment_says_what_putting_work_down_costs(self):
        self.assertIn("who it lands on", self.body)
        self.assertIn("what breaks if nobody takes it", self.body)

    def test_it_names_the_file_a_stop_is_written_to(self):
        self.assertIn(".chief-of-stuff/stop.md", self.body)

    def test_a_correct_refusal_is_still_worth_more_than_a_wrong_edit(self):
        self.assertIn("worth more than a wrong edit", self.body)


class PositiveHalfTest(unittest.TestCase):
    """Prohibitions generalise and grants do not, so a fleet ratchets toward refusal. The grants are written as sharply."""

    def setUp(self):
        self.tmp, self.root = workspace()
        self.addCleanup(self.tmp.cleanup)
        self.body = dp.compose(self.root, "2026-09-18", "Security audit")

    def test_two_categories_and_no_third(self):
        self.assertIn("git revert", self.body)
        self.assertIn("no third", self.body)

    def test_a_revertible_change_in_this_tree_needs_nobody(self):
        self.assertIn("needs nobody's word", self.body)

    def test_a_relay_never_carries_an_irreversible_one(self):
        self.assertIn("never travels on a relay", self.body)
        self.assertIn("evidence about the past", self.body)

    def test_not_handed_is_not_a_boundary(self):
        self.assertIn("not one of the two", self.body)

    def test_the_commit_line_is_zachs_rule_not_its_opposite(self):
        """Zach, 2026-09-18 23:00: every session makes meaningful small commits; uncommitted work is how work gets lost."""
        self.assertNotIn("do not commit", self.body)
        self.assertIn("Commit small and often", self.body)
        self.assertIn("never merge", self.body)

    def test_owns_is_a_duty_with_a_failure_mode(self):
        self.assertIn("yours to keep true", self.body)
        self.assertIn("your error", self.body)

    def test_a_flagged_problem_in_your_own_paths_is_your_assignment(self):
        self.assertIn("is your assignment", self.body)
        self.assertIn("do not act", self.body)

    def test_the_two_errors_are_priced(self):
        self.assertIn("costs one `git revert`", self.body)
        self.assertIn("costs the deliverable", self.body)

    def test_owns_none_gains_no_duty_clause(self):
        tracker = (self.root / "daily" / "2026-09-18-tracker.md")
        tracker.write_text(tracker.read_text().replace(
            "| Security audit | `src/a/`, `notes/audit.md` |", "| Security audit | none |"))
        body = dp.compose(self.root, "2026-09-18", "Security audit")
        self.assertIn("Owns: none", body)
        self.assertNotIn("yours to keep true", body.split("Owns: none")[1])


class StopKindsTest(unittest.TestCase):
    """Four stops take four routes. Arriving as indistinguishable English is how they landed as prose to interpret."""

    def test_the_assignment_names_the_four_kinds(self):
        tmp, root = workspace()
        self.addCleanup(tmp.cleanup)
        body = dp.compose(root, "2026-09-18", "Security audit")
        for kind in ("permission", "authorization", "ownership", "scope"):
            self.assertIn(kind, body)
