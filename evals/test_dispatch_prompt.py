"""Unit tests for scripts/dispatch_prompt.py: the assignment a spawned session wakes up holding.

The prompt is derived, never authored. Nothing reaches a spawned session that is not already in the
tracker the user approved, which is why every field here is read back off disk rather than passed
in. The one variable argument is the task name, and a task name that is not a row is refused — so a
shell expansion that reached this far produces a refusal rather than a payload.
"""

import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import dispatch_prompt as dp  # noqa: E402
import inbox  # noqa: E402

CLAUDE = """# Workspace

## Coordinator

- User: Robin
- Daily log dir: `daily/`
- Tracker: `daily/<date>-tracker.md`
- Worktrees: `trees/`
- Timezone: America/Chicago
"""

TRACKER = """# Tracker 2026-09-18

## Tasks

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

    def test_it_names_the_task_as_the_tracker_spells_it(self):
        body = dp.compose(self.root, "2026-09-18", "Security audit")
        self.assertIn("Task: Security audit", body)

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

    def test_it_still_says_which_workspace_the_task_lives_in(self):
        body = dp.compose(self.root, "2026-09-18", "Security audit")
        self.assertIn(f"Workspace: {self.root.resolve()}", body)

    def test_a_task_that_is_not_a_row_is_refused(self):
        """The one variable argument, and the reason an expansion cannot become a payload."""
        for absent in ("Upload handler fix", "$(whoami)", "`id`", ""):
            with self.assertRaises(dp.RefusedError, msg=absent):
                dp.compose(self.root, "2026-09-18", absent)


    def test_a_task_that_already_has_an_owner_is_refused(self):
        """Two trees were handed the same task and the same file. The rule said unassigned; nothing checked.

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
            dp.compose(self.root, "2026-09-18", "Nonexistent task")
        self.assertIn("Nonexistent task", str(e.exception))
        self.assertIn("2026-09-18-tracker.md", str(e.exception))

    def test_a_missing_tracker_is_refused_rather_than_composed_around(self):
        with self.assertRaises(dp.RefusedError):
            dp.compose(self.root, "2026-09-19", "Security audit")


class AssignmentTest(unittest.TestCase):
    """The remaining lines. Two lines were enough for a session to find its way and not to act."""

    def setUp(self):
        self.tmp, self.root = workspace()
        self.addCleanup(self.tmp.cleanup)

    def body(self, task="Security audit", **kw):
        return dp.compose(self.root, "2026-09-18", task, **kw)

    def test_the_whole_item_cell_travels_as_the_ask(self):
        """The item is the ask. A session that reads only that row has to know what done looks like."""
        ask = "Security audit: the upload handler. Done when every path is checked and the findings are in notes/audit.md"
        tmp, root = workspace(TRACKER.replace("| Security audit | unassigned", f"| {ask} | unassigned"))
        self.addCleanup(tmp.cleanup)
        body = dp.compose(root, "2026-09-18", ask)
        self.assertIn("Done when every path is checked", body)

    def test_it_names_the_paths_the_task_owns(self):
        self.assertIn("Owns: `src/a/`, `notes/audit.md`", self.body())

    def test_owning_nothing_is_said_rather_than_left_out(self):
        tmp, root = workspace(TRACKER.replace("| Security audit | `src/a/`, `notes/audit.md` |",
                                              "| Security audit | none |"))
        self.addCleanup(tmp.cleanup)
        self.assertIn("Owns: none", dp.compose(root, "2026-09-18", "Security audit"))

    def test_a_task_with_no_file_ownership_row_is_refused(self):
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

        Found by hand: a task called `Tab check: ...` refused with a key ending in an ellipsis, which
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
        self.assertIn("never push to main or merge locally", self.body())

    def test_it_says_what_to_reply_with(self):
        self.assertRegex(self.body(), r"(?m)^Report: \S")

    def test_the_requirement_the_task_serves_travels_when_there_is_one(self):
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

    def test_it_says_to_hand_back_a_task_that_does_not_add_up(self):
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
            task = "Security audit (via 4821-audit)" if "Security audit (via" in spoiled else "Security audit"
            with self.assertRaises(dp.RefusedError):
                dp.compose(root, "2026-09-18", task)

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
        self.assertIn("never push to main or merge locally", self.body)

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


class NameRuleTravelsTest(unittest.TestCase):
    """Finding 91: the workspace name rule does not reach a subagent a session spawns, and a commit message is durable."""

    def setUp(self):
        self.tmp, self.root = workspace()
        self.addCleanup(self.tmp.cleanup)
        self.body = dp.compose(self.root, "2026-09-18", "Security audit")

    def test_the_assignment_names_the_user_as_the_only_name(self):
        self.assertIn("Robin is the only name", self.body)

    def test_it_says_where_the_wrong_name_is_durable(self):
        self.assertIn("commit message", self.body)

    def test_it_carries_the_rule_into_anything_the_session_spawns(self):
        self.assertIn("carry this rule into it", self.body)

    def test_the_rule_never_spells_a_name_of_its_own(self):
        """The rule is stated without an example, because an example would be the thing it forbids."""
        self.assertNotRegex(self.body, r"(?i)\b(legal|birth|given) name is\b")


class ChallengedConstraintTest(unittest.TestCase):
    """impl-2, 04:00: capitulating to a confidently stated rule is the same failure as ignoring a well-founded one."""

    def setUp(self):
        self.tmp, self.root = workspace()
        self.addCleanup(self.tmp.cleanup)
        self.body = dp.compose(self.root, "2026-09-18", "Security audit")

    def test_a_line_you_are_told_you_crossed_gets_sourced_first(self):
        self.assertIn("where it comes from", self.body)

    def test_the_users_own_rules_are_accepted_at_once(self):
        self.assertIn("accept it at once", self.body)

    def test_a_constraint_that_traces_to_a_hand_off_is_said_so_not_agreed_to(self):
        self.assertIn("say so and let it be ruled on", self.body)


class ReportIsNotAStateChangeTest(unittest.TestCase):
    """arch, 04:32: sending the summary felt like finishing the work, and the next item never started."""

    def setUp(self):
        self.tmp, self.root = workspace()
        self.addCleanup(self.tmp.cleanup)
        self.body = dp.compose(self.root, "2026-09-18", "Security audit")

    def test_the_report_line_demands_one_of_two_named_states(self):
        self.assertRegex(self.body, r"(?m)^Report:.*`Next:`")
        self.assertIn("idle and available", self.body)

    def test_the_header_says_why_a_report_feels_terminal(self):
        self.assertIn("A report is not a state change", self.body)

    def test_an_unnamed_next_reads_as_idle_rather_than_as_working(self):
        self.assertIn("assume you are idle", self.body)


class InboxMetadataLineTest(unittest.TestCase):
    """Dual-transport metadata line pointing to the inbox utility."""

    def setUp(self):
        self.tmp, self.root = workspace()
        self.addCleanup(self.tmp.cleanup)
        self.body = dp.compose(self.root, "2026-09-18", "Security audit")

    def test_inbox_metadata_line_is_present_in_compose_output(self):
        expected_line = f"Inbox: {Path(inbox.__file__).resolve()}"
        self.assertIn(expected_line, self.body)

    def test_the_inbox_line_names_a_script_that_exists(self):
        """It named `<workspace>/scripts/inbox.py`, which a workspace never has; the script ships in the plugin."""
        line = next(x for x in self.body.splitlines() if x.startswith("Inbox: "))
        self.assertTrue(Path(line.removeprefix("Inbox: ")).is_file(), line)

    def test_inbox_metadata_line_format(self):
        self.assertRegex(self.body, r"(?m)^Inbox: .*/scripts/inbox\.py$")


class DualTransportFallbackAddressingTest(unittest.TestCase):
    """Fallback CLI commands must always target the role-based 'coordinator' mailbox."""

    def setUp(self):
        self.tmp, self.root = workspace()
        self.addCleanup(self.tmp.cleanup)

    def test_fallback_commands_target_coordinator_when_no_coordinator_specified(self):
        body = dp.compose(self.root, "2026-09-18", "Security audit")
        self.assertIn('send --to "coordinator" --from "<your_ref_or_name>" --type register', body)
        self.assertIn('send --to "coordinator" --from "<your_ref_or_name>" --type ask', body)
        self.assertIn('send --to "coordinator" --from "<your_ref_or_name>" --type stop', body)
        self.assertIn('send --to "coordinator" --from "<your_ref_or_name>" --type progress', body)

    def test_fallback_commands_always_target_coordinator_when_coordinator_session_passed(self):
        coord_session = "my-coord [123456]"
        body = dp.compose(self.root, "2026-09-18", "Security audit", coordinator=coord_session)
        # IPC prose retains the specific session name
        self.assertIn(f"the chief-of-stuff coordinator `{coord_session}`", body)
        # Mailbox fallback CLI commands always specify --to "coordinator"
        self.assertIn('send --to "coordinator" --from "<your_ref_or_name>" --type register', body)
        self.assertIn('send --to "coordinator" --from "<your_ref_or_name>" --type ask', body)
        self.assertIn('send --to "coordinator" --from "<your_ref_or_name>" --type stop', body)
        self.assertIn('send --to "coordinator" --from "<your_ref_or_name>" --type progress', body)
        # Ensure the ephemeral session name is never used as mailbox address
        self.assertNotIn('--to "my-coord', body)
        self.assertNotIn(f'--to "{coord_session}"', body)

    def test_all_four_fallback_command_types_present_with_coordinator_recipient(self):
        body = dp.compose(self.root, "2026-09-18", "Security audit", coordinator="coord-alpha [abcdef]")
        for msg_type in ("register", "ask", "stop", "progress"):
            pattern = rf'send --to "coordinator" --from "<your_ref_or_name>" --type {msg_type}'
            self.assertRegex(body, pattern)

    def test_fallback_command_delivers_to_coordinator_mailbox(self):
        mailbox_dir = self.root / ".chief-of-stuff" / "mailbox"
        inbox.send_message(
            recipient="coordinator",
            sender="worker-test [999999]",
            msg_type="register",
            body="ref: worker-test, task: Security audit, state: planning",
            mailbox_dir=mailbox_dir,
        )
        unread = inbox.list_messages(recipient="coordinator", mailbox_dir=mailbox_dir, unread_only=True)
        self.assertEqual(len(unread), 1)
        self.assertEqual(unread[0].type, "register")
        self.assertEqual(unread[0].sender, "worker-test [999999]")


class PromptCharacterBoundsTest(unittest.TestCase):
    """Assignment prompt character length must stay bounded within BODY_CAP."""

    def setUp(self):
        self.tmp, self.root = workspace()
        self.addCleanup(self.tmp.cleanup)

    def test_standard_assignment_is_well_under_body_cap(self):
        body = dp.compose(self.root, "2026-09-18", "Security audit")
        self.assertLess(len(body), dp.BODY_CAP)
        self.assertGreater(len(body), 5000)

    def test_large_assignment_with_max_realistic_content_stays_under_cap(self):
        item = "Security audit " + ("x" * 1600)
        chk = "Checklist: " + ("y" * 1000)
        owns = "`src/" + ("z" * 500) + "`"
        large_tracker = f"""# Tracker 2026-09-18
## Tasks
| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
| {item} | unassigned | open | 09:00 |  | {chk} |

## File ownership
| context | paths |
|---|---|
| {item} | {owns} |

## Log
- 09:00 opened
"""
        (self.root / "daily" / "2026-09-18-tracker.md").write_text(large_tracker)
        body = dp.compose(self.root, "2026-09-18", item)
        self.assertLessEqual(len(body), dp.BODY_CAP)

    def test_excessive_assignment_exceeding_cap_raises_refused_error(self):
        item = "Security audit " + ("x" * 2000)
        chk = "Checklist: " + ("y" * 2000)
        owns = "`src/" + ("z" * 2000) + "`"
        oversized_tracker = f"""# Tracker 2026-09-18
## Tasks
| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
| {item} | unassigned | open | 09:00 |  | {chk} |

## File ownership
| context | paths |
|---|---|
| {item} | {owns} |

## Log
- 09:00 opened
"""
        (self.root / "daily" / "2026-09-18-tracker.md").write_text(oversized_tracker)
        with self.assertRaises(dp.RefusedError) as ctx:
            dp.compose(self.root, "2026-09-18", item)
        self.assertIn(f"over the {dp.BODY_CAP} cap", str(ctx.exception))


class CoordinatorPromptWorkflowTest(unittest.TestCase):
    """Coordinator prompt workflow rules in agents/chief-of-stuff.md."""

    def setUp(self):
        self.agent_file = Path(__file__).resolve().parent.parent / "agents" / "chief-of-stuff.md"
        self.assertTrue(self.agent_file.exists(), f"Missing {self.agent_file}")
        self.content = self.agent_file.read_text()

    def test_resume_step_2_script_paths_are_fully_qualified(self):
        self.assertIn("python3 ${CLAUDE_PLUGIN_ROOT}/scripts/inbox.py read <id> --ack", self.content)
        self.assertIn("python3 ${CLAUDE_PLUGIN_ROOT}/scripts/inbox.py drain --recipient coordinator", self.content)

    def test_resume_step_2_task_state_uses_waiting_or_orphaned_not_stopped(self):
        self.assertIn("stops update task state to `waiting` or `orphaned`", self.content)
        self.assertNotIn("stops update task state to `stopped` or `orphaned`", self.content)

    def test_dual_transport_sending_and_registration_rules(self):
        self.assertIn("Dual-transport sending", self.content)
        self.assertIn("python3 ${CLAUDE_PLUGIN_ROOT}/scripts/inbox.py send --to <recipient> --from coordinator", self.content)
        self.assertIn("Dual-transport registration", self.content)
        self.assertIn("python3 ${CLAUDE_PLUGIN_ROOT}/scripts/inbox.py list --recipient coordinator --unread", self.content)

    def test_a_republish_never_opens_the_board(self):
        # Zach, 2026-09-23 21:34: "it keeps relaunching the artifact on me. UPDATE the artifact. I know how to open it."
        self.assertIn("Never call the tool's `open` action on the board", self.content)

    def test_the_pipeline_sends_the_reviewer_the_pr(self):
        # #33 stage 3: a lane runs through the reviewer session and the user's gates.
        self.assertIn("## Pipeline", self.content)
        self.assertIn("`review PR <url>`", self.content)
        self.assertIn("A gate yes covers every stage up to the next gate", self.content)

    def test_triage_is_the_users(self):
        self.assertIn("## Triage", self.content)
        self.assertIn("every disposition in one reply", self.content)
        self.assertIn("Never dispose of a finding for the user", self.content)

    def test_every_handed_task_opens_plan_mode(self):
        # Zach, 2026-09-23 22:25: "tasks passed to implementers should cause the implementer to enter plan mode for the new task".
        line = "`Plan: enter plan mode (EnterPlanMode) for this task before anything else; write nothing until the user approves the plan.`"
        assign = self.content.split("## Assign", 1)[1].split("\n## ", 1)[0]
        triage = self.content.split("## Triage", 1)[1].split("\n## ", 1)[0]
        self.assertIn(line, assign)
        self.assertIn(line, triage)

    def test_no_session_applies_its_own_review(self):
        self.assertNotIn("then apply them on the same branch", self.content)
        self.assertNotIn("review your pull request with ponytail-review", self.content)


# Zach, 2026-09-22 22:20: each task "is actually backed by an entry in github"; a task with none is refused.
ISSUE_CLAUDE = CLAUDE + "- Backlog: GitHub issues; repo https://github.com/o/backlog (private)\n"

ISSUE_TRACKER = """# Tracker 2026-09-18

## Tasks

| name | item | owner | state | since | due | size | issue | checklist |
|---|---|---|---|---|---|---|---|---|
| Audit | Security audit | unassigned | open | 09:00 |  | M | #12 | Checklist: Security audit of the upload handler |
| Bare | Unfiled task | unassigned | open | 09:00 |  | S |  | Checklist: unfiled |
| Garbled | Garbled task | unassigned | open | 09:00 |  | S | soon | Checklist: garbled |
| impl02 — standing implementer | impl02: standing implementer. Wait idle for the next item. | unassigned | open | 09:00 |  | S |  | Checklist: impl02 standing |

## File ownership

| context | paths |
|---|---|
| Security audit | `src/a/` |
| Unfiled task | `src/b/` |
| Garbled task | `src/c/` |
| impl02: standing implementer. Wait idle for the next item. | `src/d/` |

## Log

- 09:00 opened the day
"""


class IssueDispatchTest(unittest.TestCase):
    def test_the_assignment_names_its_issue(self):
        tmp, root = workspace(ISSUE_TRACKER, ISSUE_CLAUDE)
        self.addCleanup(tmp.cleanup)
        lines = dp.compose(root, "2026-09-18", "Security audit").splitlines()
        self.assertIn("Issue: https://github.com/o/backlog/issues/12", lines)
        self.assertEqual(lines.index("Issue: https://github.com/o/backlog/issues/12"),
                         next(i for i, x in enumerate(lines) if x.startswith("Requirement: ")) + 1)

    def test_a_task_with_no_issue_is_refused(self):
        tmp, root = workspace(ISSUE_TRACKER, ISSUE_CLAUDE)
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(dp.RefusedError) as e:
            dp.compose(root, "2026-09-18", "Unfiled task")
        self.assertIn("names no issue", str(e.exception))
        self.assertIn("gh-issue new", str(e.exception))

    def test_a_cell_that_is_not_an_issue_is_refused(self):
        tmp, root = workspace(ISSUE_TRACKER, ISSUE_CLAUDE)
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(dp.RefusedError) as e:
            dp.compose(root, "2026-09-18", "Garbled task")
        self.assertIn('"soon"', str(e.exception))

    # The tracker rule: "A standing session's placeholder is not a task and takes no issue".
    def test_a_standing_placeholder_launched_as_its_own_session_needs_no_issue(self):
        tmp, root = workspace(ISSUE_TRACKER, ISSUE_CLAUDE)
        self.addCleanup(tmp.cleanup)
        body = dp.compose(root, "2026-09-18", "impl02: standing implementer. Wait idle for the next item.", name="impl02")
        self.assertNotIn("Issue: ", body)

    def test_a_standing_placeholder_for_another_session_still_needs_one(self):
        tmp, root = workspace(ISSUE_TRACKER, ISSUE_CLAUDE)
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(dp.RefusedError) as e:
            dp.compose(root, "2026-09-18", "impl02: standing implementer. Wait idle for the next item.", name="impl03")
        self.assertIn("names no issue", str(e.exception))

    def test_no_backlog_line_means_no_requirement(self):
        tmp, root = workspace(ISSUE_TRACKER, CLAUDE)
        self.addCleanup(tmp.cleanup)
        body = dp.compose(root, "2026-09-18", "Unfiled task")
        self.assertNotIn("Issue: ", body)


class SessionNameTest(unittest.TestCase):
    """Zach, 2026-09-23 00:04: "when a session gets a name, it should stick with that name. that should
    be part of the prompt every session gets". The launch gives the name; this file says it is the only one.
    """

    def setUp(self):
        self.tmp, self.root = workspace()
        self.addCleanup(self.tmp.cleanup)

    def test_the_rule_is_in_the_header(self):
        self.assertIn("**Your name is fixed.**", dp.HEADER)
        self.assertIn("never adopt it", dp.HEADER)

    def test_an_unnamed_dispatch_still_carries_the_rule(self):
        body = dp.compose(self.root, "2026-09-18", "Security audit")
        self.assertIn("**Your name is fixed.**", body)
        self.assertNotIn("You are `", body)
        self.assertIn("<your_ref_or_name>", body)

    def test_a_name_is_stated_and_signs_every_mailbox_command(self):
        body = dp.compose(self.root, "2026-09-18", "Security audit", name="impl07")
        self.assertIn("You are `impl07`.", body)
        self.assertNotIn("<your_ref_or_name>", body)
        self.assertIn('--from "impl07"', body)
        self.assertIn('--recipient "impl07"', body)

    def test_a_name_that_is_not_a_bare_session_name_is_refused(self):
        for bad in ("impl 07", "-x", "a;b", "impl07 [abc123]", "$(whoami)", ""):
            with self.subTest(bad=bad), self.assertRaises(dp.RefusedError):
                dp.compose(self.root, "2026-09-18", "Security audit", name=bad)

    def test_the_cli_takes_a_name(self):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = dp.main(["--root", str(self.root), "--date", "2026-09-18", "--task", "Security audit",
                            "--name", "impl07"])
        self.assertEqual(code, 0)
        self.assertIn("You are `impl07`.", buf.getvalue())


class PullRequestTest(unittest.TestCase):
    """Zach, 2026-09-23 15:31: "all code changes should require a PR" … "This will fully supersede CIMP".

    The assignment is the only rulebook a dispatched session is handed, so the route to main is in it.
    """

    def setUp(self):
        self.tmp, self.root = workspace()
        self.addCleanup(self.tmp.cleanup)
        self.body = dp.compose(self.root, "2026-09-18", "Security audit")

    def test_code_reaches_main_only_through_a_pull_request(self):
        line = [x for x in self.body.splitlines() if x.startswith("Commits: ")][0]
        self.assertIn("only through a pull request", line)
        self.assertIn("gh pr create", line)
        self.assertIn("glab mr create", line)
        self.assertIn("never push to main or merge locally", line)

    def test_the_session_opens_it_when_green(self):
        """Zach, 2026-09-23 19:40: "I don't want to stop for PR … PRs should be autononmous"."""
        line = [x for x in self.body.splitlines() if x.startswith("Commits: ")][0]
        self.assertIn("when the work is finished and its suite is green", line)
        self.assertNotIn("when you are told to", line)

    def test_the_commit_rule_survives(self):
        self.assertIn("Commit small and often", self.body)

    def test_cimp_is_not_named(self):
        self.assertNotIn("CIMP", self.body)
        self.assertNotRegex(self.body, r"\bCIP\b")


class PonytailTest(unittest.TestCase):
    """Zach, 2026-09-23 17:01: ponytail "a core part of this" — in the dispatch, a review on every PR, ultra."""

    def setUp(self):
        self.tmp, self.root = workspace()
        self.addCleanup(self.tmp.cleanup)
        self.body = dp.compose(self.root, "2026-09-18", "Security audit")

    def test_the_header_names_ponytail_at_ultra(self):
        self.assertIn("**ponytail, ultra.**", self.body)

    def test_a_new_task_is_a_new_plan(self):
        """Zach, 2026-09-23 22:25: a later task handed to this session starts in plan mode too."""
        self.assertIn("**A new task is a new plan.**", self.body)
        agy = dp.compose(self.root, "2026-09-18", "Security audit", name="implementer-GFlash38H-01", runtime="agy")
        self.assertIn("**A new task is a new plan.**", agy)

    def test_the_users_ask_is_still_built(self):
        """House rule 9: a challenge is said once, then the instruction is built."""
        self.assertIn("then build what Robin asked", self.body)

    def test_the_failing_test_still_comes_first(self):
        self.assertIn("The failing test comes first", self.body)

    def test_the_session_reports_the_pr_and_stops(self):
        """#33 stage 3 (Zach, 2026-09-23: "Reviewer + my triage"): the reviewer session reviews, Zach triages."""
        line = [x for x in self.body.splitlines() if x.startswith("Commits: ")][0]
        self.assertIn("report its URL and head sha to the coordinator, then stop", line)

    def test_no_self_review_and_no_self_applied_cuts(self):
        line = [x for x in self.body.splitlines() if x.startswith("Commits: ")][0]
        self.assertNotIn("ponytail-review", line)
        self.assertNotIn("apply the cuts", line)
        self.assertIn("a finding is fixed only when the coordinator sends it to you", line)


class OnlyTheUserMergesTest(unittest.TestCase):
    """Zach, 2026-09-23 17:26: "I'll review and click the auto merge button on all PRs from here on forward."

    The line used to say the merge waits for a word, which read as a session merging once it had one.
    """

    def test_the_session_never_merges(self):
        tmp, root = workspace()
        self.addCleanup(tmp.cleanup)
        line = [x for x in dp.compose(root, "2026-09-18", "Security audit").splitlines() if x.startswith("Commits: ")][0]
        self.assertIn("The merge is the user's alone: you never merge a pull request.", line)
        self.assertNotIn("word you were given", line)


class AgyRuntimeTest(unittest.TestCase):
    """An agy session cannot list sessions or message one: the mailbox is its only channel."""

    NAME = "implementer-GFlash38H-01"

    def setUp(self):
        self.tmp, self.root = workspace()
        self.addCleanup(self.tmp.cleanup)

    def test_the_agy_paragraph_is_present_for_agy_only(self):
        agy = dp.compose(self.root, "2026-09-18", "Security audit", name=self.NAME, runtime="agy")
        claude = dp.compose(self.root, "2026-09-18", "Security audit", name=self.NAME)
        self.assertIn("**You run under Antigravity, not Claude Code.**", agy)
        self.assertNotIn("Antigravity", claude)

    def test_it_names_its_mailbox_and_no_self_review(self):
        body = dp.compose(self.root, "2026-09-18", "Security audit", name=self.NAME, runtime="agy")
        para = body.split("**You run under Antigravity, not Claude Code.**", 1)[1].split("\n\n", 1)[0]
        self.assertIn(f'--recipient "{self.NAME}" --unread', para)
        self.assertNotIn("ponytail-review", para)
        self.assertIn(f'--from "{self.NAME}"', body)


class MailboxRootTest(unittest.TestCase):
    """The inbox finds its mailbox through git's common dir, so a session in a fork worktree resolved the
    fork's mailbox while the coordinator, in a workspace that is no repo, read the workspace's. Every
    inbox command names the coordinator's mailbox, so a mailbox-only session is heard."""

    def test_every_inbox_command_names_the_workspace_mailbox(self):
        tmp, root = workspace()
        self.addCleanup(tmp.cleanup)
        body = dp.compose(root, "2026-09-18", "Security audit", name="implementer-GFlash38H-01", runtime="agy")
        want = f"--mailbox-dir {root.resolve() / '.chief-of-stuff' / 'mailbox'}"
        commands = [c for c in re.findall(r"`python3 [^`]*inbox\.py[^`]*`", body)]
        self.assertTrue(commands)
        for c in commands:
            self.assertIn(want, c)
