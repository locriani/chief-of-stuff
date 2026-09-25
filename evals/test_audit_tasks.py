"""Unit tests for scripts/audit_tasks.py: a task is done when its change is on main, and a script says so, not a grep.

Repos here are real: a bare origin, a clone that has fetched it, and worktrees off that clone.
`git merge-base --is-ancestor` and `git status --porcelain` have no useful fake, and the point of the
script is that its answer is git's answer.
"""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import audit_tasks as al  # noqa: E402
from render_board import short_name  # noqa: E402

CLAUDE = """# Workspace

## Coordinator

- User: Robin
- Daily log dir: `daily/`
- Tracker: `daily/<date>-tracker.md`
- Worktrees: `trees/`
- Timezone: America/Chicago
"""

TRACKER = """# Tracker 2026-09-17

Coordinator: coordinator. Board: none.

## Tasks

| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
{rows}

## Decisions

| time | item | Robin's words |
|---|---|---|

## Sessions

| ref | name | state | doing | waiting on | free at | constraints | children | last reply |
|---|---|---|---|---|---|---|---|---|
{sessions}

## File ownership

| context | paths |
|---|---|
{ownership}

## Log

- 09:00 opened the day
"""


def git(*args: str, cwd: Path) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd), "GIT_TERMINAL_PROMPT": "0"},
    )
    return out.stdout.strip()


def build(root: Path) -> dict[str, Path]:
    """A bare origin, a clone with origin/main fetched, and three worktrees: merged, unmerged, dirty."""
    origin = root / "origin.git"
    origin.mkdir()
    git("init", "--bare", "--initial-branch=main", ".", cwd=origin)

    clone = root / "repo"
    clone.mkdir()
    git("init", "--initial-branch=main", ".", cwd=clone)
    git("config", "user.email", "t@example.test", cwd=clone)
    git("config", "user.name", "Test", cwd=clone)
    (clone / "README.md").write_text("base\n")
    git("add", "README.md", cwd=clone)
    git("commit", "-m", "base", cwd=clone)
    git("remote", "add", "origin", str(origin), cwd=clone)
    git("push", "-u", "origin", "main", cwd=clone)

    trees = root / "trees"
    trees.mkdir()

    # A branch whose commit is already on main: work that is genuinely finished.
    git("branch", "landed", cwd=clone)
    git("worktree", "add", str(trees / "wt-merged"), "landed", cwd=clone)

    # A branch with a commit of its own, never merged.
    git("worktree", "add", "-b", "feat/open", str(trees / "wt-unmerged"), "main", cwd=clone)
    (trees / "wt-unmerged" / "new.txt").write_text("work\n")
    git("add", "new.txt", cwd=trees / "wt-unmerged")
    git("commit", "-m", "work", cwd=trees / "wt-unmerged")

    # A branch on main, but with the work still uncommitted.
    git("worktree", "add", "-b", "feat/dirty", str(trees / "wt-dirty"), "main", cwd=clone)
    (trees / "wt-dirty" / "scratch.txt").write_text("unsaved\n")

    return {"origin": origin, "clone": clone, "trees": trees}


def session_row(name: str, ref: str = "a1b2c3") -> str:
    return f"| {ref} | {name} | working | a task | | now | | none | 09:30 |"


def workspace(rows: str, ownership: str, sessions: str = "") -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "daily").mkdir()
    (root / "CLAUDE.md").write_text(CLAUDE)
    (root / "daily" / "2026-09-17-tracker.md").write_text(
        TRACKER.format(rows=rows, ownership=ownership, sessions=sessions))
    build(root)
    return tmp, root


class ConfigTest(unittest.TestCase):
    def test_worktrees_line_read_from_the_coordinator_block(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "CLAUDE.md"
            p.write_text(CLAUDE)
            self.assertEqual(al.worktrees_dir(p.read_text()), "trees/")

    def test_no_worktrees_line_means_the_root_itself(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "CLAUDE.md"
            p.write_text(CLAUDE.replace("- Worktrees: `trees/`\n", ""))
            self.assertEqual(al.worktrees_dir(p.read_text()), "")


class OwnershipTest(unittest.TestCase):
    def test_finds_the_worktree_named_in_a_prose_row(self):
        rows = al.parse_ownership(
            "| demo-fixes [b8fca1] (fix 5, from 11:13) | worktree openemr-agent-demo-fixes (fix/agent-demo-fixes off main d6aab3b) |"
        )
        self.assertEqual(rows[0].context, "demo-fixes [b8fca1] (fix 5, from 11:13)")
        self.assertEqual(rows[0].worktrees, ["openemr-agent-demo-fixes"])

    def test_a_row_with_no_worktree_yields_none(self):
        rows = al.parse_ownership("| subagent (Final requirements) | Projects/week-1/FINAL-REQUIREMENTS.md (new) |")
        self.assertEqual(rows[0].worktrees, [])

    def test_reads_a_bullet_row_with_a_backticked_worktree(self):
        rows = al.parse_ownership("- Security audit: worktree `audit-upload` \u00b7 src/a/")
        self.assertEqual(rows[0].context, "Security audit")
        self.assertEqual(rows[0].worktrees, ["audit-upload"])

    def test_a_bullet_row_with_no_worktree_yields_none(self):
        rows = al.parse_ownership("- Release notes: notes/release.md")
        self.assertEqual(rows[0].worktrees, [])

    def test_a_row_matches_by_task_item_as_well_as_owner(self):
        row = al.OwnerRow("Security audit", ["audit-upload"])
        self.assertTrue(al.owns(row, "Security audit"))
        self.assertFalse(al.owns(row, "Draft release notes"))

    def test_owner_matches_a_row_ignoring_the_ref_and_the_parenthetical(self):
        row = al.OwnerRow("demo-fixes [b8fca1] (fix 5, from 11:13)", ["openemr-agent-demo-fixes"])
        self.assertTrue(al.owns(row, "demo-fixes"))
        self.assertTrue(al.owns(row, "demo-fixes [b8fca1]"))
        self.assertFalse(al.owns(row, "demo"))
        self.assertFalse(al.owns(row, "architecture"))


class ProseTest(unittest.TestCase):
    """Ownership cells are prose, and prose says "worktree" in sentences that name no tree."""

    def test_a_word_that_follows_worktree_in_a_sentence_is_not_a_tree(self):
        rows = al.parse_ownership(
            "| 32505 | worktree openemr-agent-smart (feat/smart), that branch only: a docs subagent is on those now, its worktree only |"
        )
        self.assertEqual(rows[0].worktrees, ["openemr-agent-smart"])

    def test_trailing_punctuation_is_not_part_of_the_name(self):
        rows = al.parse_ownership("| a | worktree wt-audit, and notes/release.md |")
        self.assertEqual(rows[0].worktrees, ["wt-audit"])


class ReadabilityTest(unittest.TestCase):
    def test_a_refusal_is_reported_once_however_many_tasks_name_it(self):
        rows = (
            "| One | robin | done 10:00 | 09:00 |  | Checklist: One |\n"
            "| Two | robin | done 10:01 | 09:00 |  | Checklist: Two |\n"
            "| Three | robin | done 10:02 | 09:00 |  | Checklist: Three |"
        )
        tmp, root = workspace(rows, "| robin | worktree wt-not-here |")
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(sum(1 for line in report.lines if "wt-not-here" in line), 1, report.lines)

    def test_a_long_task_item_is_cut_short_in_the_reopen_line(self):
        item = "Delete agent-parked (superseded; secrets) and the client-JSON copy in the scratchpad. 11:13 assigned. 11:53 deleted (62 MB); path never recorded"
        tmp, root = workspace(
            f"| {item} | robin | done 10:00 | 09:00 |  | Checklist: Delete |",
            "| robin | worktree wt-unmerged (feat/open) |",
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        line = str(report.reopen[0])
        self.assertLess(len(line), 140, line)
        self.assertIn("Delete agent-parked", line)


class ContainmentTest(unittest.TestCase):
    def test_a_worktree_outside_the_root_is_refused_not_probed(self):
        tmp, root = workspace(
            "| Escape | robin | done 10:00 | 09:00 |  | Checklist: Escape |",
            "| robin | worktree ../../../etc |",
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertTrue(any("outside" in line for line in report.lines), report.lines)
        self.assertEqual(report.reopen, [])

    def test_a_missing_worktree_is_reported_not_silent(self):
        tmp, root = workspace(
            "| Gone | robin | done 10:00 | 09:00 |  | Checklist: Gone |",
            "| robin | worktree wt-not-here |",
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertTrue(any("wt-not-here" in line for line in report.lines), report.lines)


class BulletOwnershipAuditTest(unittest.TestCase):
    def test_done_task_reopens_when_its_tree_is_named_in_a_bullet(self):
        tmp, root = workspace(
            "| Open branch work | robin | done 10:00 | 09:00 |  | Checklist: Open branch work |",
            "- Open branch work: worktree `wt-unmerged` \u00b7 src/a/",
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual([r.task for r in report.reopen], ["Open branch work"])


class AuditTest(unittest.TestCase):
    def test_done_on_main_stays_done(self):
        tmp, root = workspace(
            "| Landed work | robin | done 10:00 | 09:00 |  | Checklist: Landed work |",
            "| robin | worktree wt-merged (landed) |",
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(report.reopen, [])

    def test_done_on_an_unmerged_branch_must_reopen(self):
        tmp, root = workspace(
            "| Open branch work | robin | done 10:00 | 09:00 |  | Checklist: Open branch work |",
            "| robin | worktree wt-unmerged (feat/open) |",
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual([r.task for r in report.reopen], ["Open branch work"])
        self.assertIn("not on main", report.reopen[0].why)

    def test_done_with_uncommitted_work_must_reopen(self):
        tmp, root = workspace(
            "| Unsaved work | robin | done 10:00 | 09:00 |  | Checklist: Unsaved work |",
            "| robin | worktree wt-dirty (feat/dirty) |",
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual([r.task for r in report.reopen], ["Unsaved work"])
        self.assertIn("uncommitted", report.reopen[0].why)

    def test_a_task_with_no_tree_is_never_reopened(self):
        tmp, root = workspace(
            "| Decide the merge order | robin | done 10:00 | 09:00 |  | Checklist: Decide |",
            "| robin | Projects/notes.md |",
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(report.reopen, [])

    def test_a_running_task_on_an_unmerged_branch_is_not_reopened(self):
        tmp, root = workspace(
            "| Still going | robin | running 09:30 | 09:00 |  | Checklist: Still going |",
            "| robin | worktree wt-unmerged (feat/open) |",
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(report.reopen, [])

    def test_two_done_tasks_one_landed_one_not(self):
        rows = (
            "| Landed work | robin | done 10:00 | 09:00 |  | Checklist: Landed work |\n"
            "| Open branch work | sam | done 10:30 | 09:00 |  | Checklist: Open branch work |"
        )
        ownership = "| robin | worktree wt-merged (landed) |\n| sam | worktree wt-unmerged (feat/open) |"
        tmp, root = workspace(rows, ownership)
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual([r.task for r in report.reopen], ["Open branch work"])


class PushGapTest(unittest.TestCase):
    def test_main_even_with_origin_is_reported_as_even(self):
        tmp, root = workspace(
            "| Landed work | robin | done 10:00 | 09:00 |  | Checklist: Landed work |",
            "| robin | worktree wt-merged (landed) |",
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertTrue(any("origin/main" in line and "even" in line for line in report.lines), report.lines)

    def test_a_commit_on_main_not_pushed_is_reported_as_unpushed(self):
        tmp, root = workspace(
            "| Landed work | robin | done 10:00 | 09:00 |  | Checklist: Landed work |",
            "| robin | worktree wt-merged (landed) |",
        )
        self.addCleanup(tmp.cleanup)
        clone = root / "repo"
        (clone / "later.txt").write_text("later\n")
        git("add", "later.txt", cwd=clone)
        git("commit", "-m", "later", cwd=clone)
        report = al.audit(root, "2026-09-17")
        self.assertTrue(any("unpushed" in line for line in report.lines), report.lines)


class ArgvTest(unittest.TestCase):
    def test_git_is_called_with_a_list_and_never_through_a_shell(self):
        source = (Path(__file__).resolve().parent.parent / "scripts" / "audit_tasks.py").read_text()
        self.assertNotIn("shell=True", source)
        self.assertIn('"--porcelain"', source)

    def test_exit_code_counts_the_rows_to_reopen(self):
        tmp, root = workspace(
            "| Open branch work | robin | done 10:00 | 09:00 |  | Checklist: Open branch work |",
            "| robin | worktree wt-unmerged (feat/open) |",
        )
        self.addCleanup(tmp.cleanup)
        self.assertEqual(al.main(["--root", str(root), "--date", "2026-09-17"]), 1)

    def test_a_clean_tracker_exits_zero(self):
        tmp, root = workspace(
            "| Landed work | robin | done 10:00 | 09:00 |  | Checklist: Landed work |",
            "| robin | worktree wt-merged (landed) |",
        )
        self.addCleanup(tmp.cleanup)
        self.assertEqual(al.main(["--root", str(root), "--date", "2026-09-17"]), 0)


if __name__ == "__main__":
    unittest.main()


class MultipleRowsPerContextTest(unittest.TestCase):
    """One context, several rows: the parenthetical is the only thing saying which task a row is about.

    Live shape from 2026-09-17: `architecture [a16e40]` owned both the agent-parked deletion (no tree,
    finished) and the ARCHITECTURE.md reconciliation (a tree). Matching on the bare context name gave
    the deletion task the other row's worktree and reopened a task that was genuinely done.
    """

    OWNERSHIP = (
        "| architecture [a16e40] (agent-parked deletion, 11:13-11:53, done) | Projects/week-1/agent-parked/ (deleted) |\n"
        "| architecture [a16e40] (ARCHITECTURE.md reconciliation, from 11:53) | worktree wt-unmerged (docs/arch from main): ARCHITECTURE.md |"
    )
    ROWS = (
        "| Delete agent-parked/ | architecture | done | 09:00 |  |  |\n"
        "| Reconcile ARCHITECTURE.md | architecture | done | 09:00 |  |  |"
    )

    def test_a_no_tree_task_does_not_inherit_a_sibling_rows_worktree(self):
        tmp, root = workspace(self.ROWS, self.OWNERSHIP)
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        reopened = [r.task for r in report.reopen]
        self.assertNotIn("Delete agent-parked/", reopened)

    def test_the_task_the_row_names_still_reopens(self):
        tmp, root = workspace(self.ROWS, self.OWNERSHIP)
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual([r.task for r in report.reopen], ["Reconcile ARCHITECTURE.md"])

    def test_a_task_no_row_names_is_reported_as_ambiguous_not_reopened(self):
        ownership = (
            "| pair [a1b2c3] (first thing, from 10:00) | worktree wt-unmerged (feat/open) |\n"
            "| pair [a1b2c3] (second thing, from 11:00) | worktree wt-dirty (feat/dirty) |"
        )
        tmp, root = workspace("| Something else entirely | pair | done | 09:00 |  |  |", ownership)
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(report.reopen, [])
        self.assertTrue(
            any("ambiguous" in line and "Something else entirely" in line for line in report.lines),
            report.lines,
        )

    def test_one_row_for_a_context_needs_no_parenthetical_at_all(self):
        """The common case must not regress: a single row owns everything that context owns."""
        tmp, root = workspace(
            "| Ship the thing | solo | done | 09:00 |  |  |",
            "| solo [a1b2c3] | worktree wt-unmerged (feat/open) |",
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual([r.task for r in report.reopen], ["Ship the thing"])


class OrphanJoinTest(unittest.TestCase):
    """Finding 47. The audit reports per tree, and a tree that lost its writer is a fact about owners.

    Five sessions left the registry in fifty minutes one night and three left uncommitted work. The
    script found none of it, because nothing here had ever read `## Sessions`.
    """

    DIRTY = "| Unsaved work | sam | waiting | 09:00 |  | Checklist: Unsaved work |"
    OWNS = "| sam | worktree wt-dirty (feat/dirty) |"

    def test_uncommitted_work_whose_owner_is_not_a_session_is_named(self) -> None:
        tmp, root = workspace(self.DIRTY, self.OWNS, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(len(report.orphans), 1, report.lines)
        line = str(report.orphans[0])
        self.assertIn("wt-dirty", line)
        self.assertIn("sam", line)
        self.assertIn("uncommitted", line)

    def test_a_live_owner_is_not_an_orphan(self) -> None:
        tmp, root = workspace(self.DIRTY, self.OWNS, sessions=session_row("sam"))
        self.addCleanup(tmp.cleanup)
        self.assertEqual(al.audit(root, "2026-09-17").orphans, [])

    def test_the_ref_a_listing_shows_does_not_defeat_the_join(self) -> None:
        """`_bare()` already strips `[a1b2c3]`; this is the one place both sides of the join use it."""
        tmp, root = workspace(self.DIRTY, self.OWNS, sessions=session_row("sam [a1b2c3]"))
        self.addCleanup(tmp.cleanup)
        self.assertEqual(al.audit(root, "2026-09-17").orphans, [])

    def test_no_sessions_section_makes_no_orphan_claims(self) -> None:
        """A listing is not a roster, and neither is an empty table. With no roster, say nothing."""
        tmp, root = workspace(self.DIRTY, self.OWNS)
        self.addCleanup(tmp.cleanup)
        self.assertEqual(al.audit(root, "2026-09-17").orphans, [])

    def test_committed_work_is_not_an_orphan_however_gone_its_owner(self) -> None:
        """An unmerged branch is recoverable by name. A dirty tree nobody is writing in is not."""
        tmp, root = workspace(
            "| Open branch work | sam | waiting | 09:00 |  | Checklist: Open branch work |",
            "| sam | worktree wt-unmerged (feat/open) |", sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        self.assertEqual(al.audit(root, "2026-09-17").orphans, [])

    def test_the_count_reaches_the_summary_line(self) -> None:
        tmp, root = workspace(self.DIRTY, self.OWNS, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        self.assertIn("orphaned=1", al.audit(root, "2026-09-17").lines[-1])


class MissingTreeTest(unittest.TestCase):
    """Finding 46. A missing tree means two opposite things and the script printed one line for both.

    `openemr-agent-smart` was gone because its branch merged and the session tidied up.
    `openemr-agent-defects-140c0b2` was gone because the row carried a planned name and the tree was
    made without the suffix. One is housekeeping; the other is a task pointing at nothing.
    """

    def remove(self, root: Path, name: str) -> None:
        git("worktree", "remove", "--force", str(root / "trees" / name), cwd=root / "repo")

    def test_a_merged_branch_with_no_tree_is_routine(self) -> None:
        tmp, root = workspace(
            "| Landed work | robin | done 10:00 | 09:00 |  | Checklist: Landed work |",
            "| robin | worktree wt-merged (landed) |")
        self.addCleanup(tmp.cleanup)
        self.remove(root, "wt-merged")
        line = next(ln for ln in al.audit(root, "2026-09-17").lines if "wt-merged" in ln)
        self.assertIn("landed", line)
        self.assertIn("on main", line)
        self.assertNotIn("never", line)

    def test_an_unmerged_branch_with_no_tree_is_the_alarming_one(self) -> None:
        tmp, root = workspace(
            "| Open branch work | sam | waiting | 09:00 |  | Checklist: Open branch work |",
            "| sam | worktree wt-unmerged (feat/open) |")
        self.addCleanup(tmp.cleanup)
        self.remove(root, "wt-unmerged")
        line = next(ln for ln in al.audit(root, "2026-09-17").lines if "wt-unmerged" in ln)
        self.assertIn("feat/open", line)
        self.assertIn("not on main", line)

    def test_a_name_with_no_branch_behind_it_was_never_created(self) -> None:
        tmp, root = workspace(
            "| Planned work | sam | waiting | 09:00 |  | Checklist: Planned work |",
            "| sam | worktree wt-never-made (feat/planned) |")
        self.addCleanup(tmp.cleanup)
        line = next(ln for ln in al.audit(root, "2026-09-17").lines if "wt-never-made" in ln)
        self.assertIn("no branch", line)

    def test_with_no_tree_left_to_ask_git_in_the_script_says_so(self) -> None:
        """Every answer here is git's answer, and git needs a repository to be asked in."""
        tmp, root = workspace(
            "| Planned work | sam | waiting | 09:00 |  | Checklist: Planned work |",
            "| sam | worktree wt-never-made (feat/planned) |")
        self.addCleanup(tmp.cleanup)
        for name in ("wt-merged", "wt-unmerged", "wt-dirty"):
            self.remove(root, name)
        line = next(ln for ln in al.audit(root, "2026-09-17").lines if "wt-never-made" in ln)
        self.assertIn("no worktree", line)
        self.assertNotIn("no branch", line)


class UnclaimedOwnershipRowTest(unittest.TestCase):
    """A tree reached only through a task is a tree nobody can reach once the task lets go.

    Live at 17:20 the workspace held three trees whose File ownership context was `nobody (…,
    orphaned …)`. No task owner is `nobody`, so `rows_for` matched none of them and the audit walked
    straight past all three — the very trees finding 47 was written about.
    """

    OWNS = ("| robin | worktree wt-merged (landed) |\n"
            "| nobody (the TTL fix, orphaned) | worktree wt-dirty (feat/dirty) |")
    TASK = "| Landed work | robin | done 10:00 | 09:00 |  | Checklist: Landed work |"

    def test_a_row_no_task_claims_is_still_audited(self) -> None:
        tmp, root = workspace(self.TASK, self.OWNS, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertTrue(any("wt-dirty" in ln for ln in report.lines), report.lines)

    def test_and_its_orphaned_work_is_named(self) -> None:
        tmp, root = workspace(self.TASK, self.OWNS, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(len(report.orphans), 1, report.lines)
        self.assertIn("nobody", str(report.orphans[0]))

    def test_an_unclaimed_row_never_reopens_a_task(self) -> None:
        """It belongs to no task, so there is no row to reopen and nothing to infer about one."""
        tmp, root = workspace(self.TASK, self.OWNS, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        self.assertEqual(al.audit(root, "2026-09-17").reopen, [])

    def test_a_tree_is_reported_once_however_many_rows_reach_it(self) -> None:
        tmp, root = workspace(
            "| Landed work | robin | done 10:00 | 09:00 |  | Checklist: Landed work |",
            "| robin | worktree wt-merged (landed) |\n| nobody | worktree wt-merged (landed) |",
            sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(sum(1 for ln in report.lines if ln.startswith("wt-merged ")), 1, report.lines)


class MainShaTest(unittest.TestCase):
    """House rule 5 (CIMP, 2026-09-18 17:18): a green branch is not evidence, a green main is.

    The script cannot watch a suite run, so it does not claim to. What it can do is name the commit
    the claim has to be about, so "suite green on main" becomes checkable instead of unfalsifiable:
    if main has moved since, the evidence is about a main that no longer exists.
    """

    def test_the_audit_names_the_commit_on_main_a_claim_must_pin_to(self) -> None:
        tmp, root = workspace(
            "| Landed work | robin | done 10:00 | 09:00 |  | Checklist: Landed work |",
            "| robin | worktree wt-merged (landed) |")
        self.addCleanup(tmp.cleanup)
        sha = git("rev-parse", "--short", "main", cwd=root / "repo")
        line = next(ln for ln in al.audit(root, "2026-09-17").lines if "origin/main" in ln)
        self.assertIn(sha, line)

    def test_with_no_tree_on_disk_there_is_no_sha_to_report(self) -> None:
        """Every answer here is git's answer, and there is nowhere left to ask."""
        tmp, root = workspace(
            "| Planned work | sam | waiting | 09:00 |  | Checklist: Planned work |",
            "| sam | worktree wt-never-made (feat/planned) |")
        self.addCleanup(tmp.cleanup)
        self.assertFalse([ln for ln in al.audit(root, "2026-09-17").lines if "origin/main" in ln])


STOP = """Stop: permission
Task: {task}
Lands on: Robin
Costs: D4 does not reach main, and the Final submission is short a deliverable
"""


class StopTest(unittest.TestCase):
    """A stop is an artifact, not a message: a message is lost if nobody reads it, a file is not."""

    def setUp(self):
        self.tmp, self.root = workspace(
            "| Merge D4 | impl-3 | running 02:20 | 09:00 |  | c |",
            "| Merge D4 | worktree wt-unmerged (feat/open) |",
            session_row("impl-3"))
        stop = self.root / "trees" / "wt-unmerged" / al.PROMPT_DIR
        stop.mkdir(parents=True)
        (stop / "stop.md").write_text(STOP.format(task="Merge D4"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_stop_against_a_running_task_is_reported(self):
        report = al.audit(self.root, "2026-09-17")
        self.assertEqual([s.task for s in report.stopped], ["Merge D4"])
        self.assertEqual(report.stopped[0].lands_on, "Robin")

    def test_the_line_says_the_kind_and_who_it_lands_on(self):
        line = str(al.audit(self.root, "2026-09-17").stopped[0])
        self.assertIn("stopped:", line)
        self.assertIn("permission", line)
        self.assertIn("Robin", line)

    def test_the_summary_counts_it_and_the_exit_code_does_too(self):
        report = al.audit(self.root, "2026-09-17")
        self.assertIn("stopped=1", report.lines[-1])
        self.assertEqual(al.main(["--root", str(self.root), "--date", "2026-09-17"]), 1)

    def test_a_stop_whose_task_is_already_open_is_not_a_fault(self):
        tracker = self.root / "daily" / "2026-09-17-tracker.md"
        tracker.write_text(tracker.read_text().replace("running 02:20", "open"))
        report = al.audit(self.root, "2026-09-17")
        self.assertEqual(report.stopped, [])
        self.assertIn("stopped=0", report.lines[-1])

    def test_a_tree_with_no_stop_says_nothing(self):
        (self.root / "trees" / "wt-unmerged" / al.PROMPT_DIR / "stop.md").unlink()
        self.assertEqual(al.audit(self.root, "2026-09-17").stopped, [])


class StopEdgesTest(unittest.TestCase):
    """A session that half-wrote a stop still stopped, and a stop somebody acted on is not a fault."""

    def setUp(self):
        self.tmp, self.root = workspace(
            "| Merge D4 | impl-3 | running 02:20 | 09:00 |  | c |",
            "| Merge D4 | worktree wt-unmerged (feat/open) |",
            session_row("impl-3"))
        self.dir = self.root / "trees" / "wt-unmerged" / al.PROMPT_DIR
        self.dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_stop_with_no_kind_is_reported_as_unstated_not_ignored(self):
        (self.dir / "stop.md").write_text("Task: Merge D4\nLands on: Robin\n")
        line = str(al.audit(self.root, "2026-09-17").stopped[0])
        self.assertIn("kind unstated", line)

    def test_a_stop_naming_nobody_says_so(self):
        (self.dir / "stop.md").write_text("Stop: scope\nTask: Merge D4\n")
        self.assertIn("lands on nobody it named", str(al.audit(self.root, "2026-09-17").stopped[0]))

    def test_an_empty_stop_file_still_counts(self):
        (self.dir / "stop.md").write_text("")
        self.assertEqual(len(al.audit(self.root, "2026-09-17").stopped), 1)


QUEUE = """
## Decision queue

| # | decision | why it is next | who is blocked |
|---|---|---|---|
{rows}
"""


def with_queue(root: Path, rows: str) -> None:
    t = root / "daily" / "2026-09-17-tracker.md"
    t.write_text(t.read_text() + QUEUE.format(rows=rows))


class DecisionQueueTest(unittest.TestCase):
    """Emitting N asks is emitting none, so the queue has to be one ordered list with a payload per row."""

    def setUp(self):
        self.tmp, self.root = workspace("| A | Robin | open | 09:00 |  | c |", "| A | none |")
        self.addCleanup(self.tmp.cleanup)

    def test_a_clean_queue_reports_its_head_and_no_fault(self):
        with_queue(self.root, "| 1 | Ship it | it blocks two | impl-2 |\n| 2 | Later | it does not | nobody |")
        report = al.audit(self.root, "2026-09-17")
        self.assertEqual(report.queue, [])
        self.assertIn("queued=2", report.lines[-1])
        self.assertIn("next decision: 1 — Ship it", "\n".join(report.lines))

    def test_two_rows_with_the_same_number_are_a_fault(self):
        with_queue(self.root, "| 1 | Ship it | x | y |\n| 4 | One | x | y |\n| 4 | Another | x | y |")
        self.assertIn("number 4 twice", "\n".join(str(q) for q in al.audit(self.root, "2026-09-17").queue))

    def test_an_answered_row_that_never_left_is_a_fault(self):
        with_queue(self.root, "| ~~1~~ | ~~Done~~ **ANSWERED 03:29** | — | — |\n| 2 | Ship it | x | y |")
        self.assertIn("answered", "\n".join(str(q) for q in al.audit(self.root, "2026-09-17").queue))

    def test_a_row_with_no_payload_cannot_be_decided(self):
        with_queue(self.root, "| 1 | Ship it |  |  |")
        self.assertIn("names neither", "\n".join(str(q) for q in al.audit(self.root, "2026-09-17").queue))

    def test_faults_reach_the_exit_code(self):
        with_queue(self.root, "| 4 | One | x | y |\n| 4 | Another | x | y |")
        self.assertEqual(al.main(["--root", str(self.root), "--date", "2026-09-17"]), 1)

    def test_no_queue_section_says_nothing_at_all(self):
        report = al.audit(self.root, "2026-09-17")
        self.assertEqual(report.queue, [])
        self.assertNotIn("queued=", report.lines[-1])

    def test_the_next_line_names_a_decision_that_opens_with_a_bold_headline(self):
        """`short_name` cutting inside a `**` pair returns a bare ellipsis, and the whole point of the line is the name."""
        with_queue(self.root, "| 3 | **Which surface carries the cache hit rate.** impl-1 measured first | x | y |")
        self.assertIn("next decision: 3 — Which surface carries the cache hit rate",
                      "\n".join(al.audit(self.root, "2026-09-17").lines))


class StopReadGuardTest(unittest.TestCase):
    """A stop file is written by a session and printed into somebody's terminal. It is checked like a dispatch field."""

    def setUp(self):
        self.tmp, self.root = workspace(
            "| Merge D4 | impl-3 | running 02:20 | 09:00 |  | c |",
            "| Merge D4 | worktree wt-unmerged (feat/open) |",
            session_row("impl-3"))
        self.dir = self.root / "trees" / "wt-unmerged" / al.PROMPT_DIR
        self.dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_stop_that_exists_but_cannot_be_read_still_counts(self):
        """Fail-open is the one thing this feature cannot do: a stop it could not read is still a stop."""
        (self.dir / "stop.md").mkdir()  # a directory where the file goes: exists, unreadable as text
        report = al.audit(self.root, "2026-09-17")
        self.assertEqual(len(report.stopped), 1)
        self.assertIn("unreadable", str(report.stopped[0]))

    def test_an_escape_payload_never_reaches_the_terminal(self):
        (self.dir / "stop.md").write_text("Stop: permission\nTask: Merge D4\nLands on: \x1b]0;pwned\x07Robin\n")
        line = str(al.audit(self.root, "2026-09-17").stopped[0])
        self.assertNotIn("\x1b", line)
        self.assertNotIn("\x07", line)

    def test_a_field_long_enough_to_bury_the_report_is_clipped(self):
        (self.dir / "stop.md").write_text("Stop: permission\nTask: Merge D4\nLands on: " + "x" * 5000 + "\n")
        self.assertLess(len(str(al.audit(self.root, "2026-09-17").stopped[0])), 1000)


class PushGapNamesMainTest(unittest.TestCase):
    """The one sha the CIMP gate rests on. `Verified` is re-checkable only if it names the branch it claims to."""

    def setUp(self):
        self.tmp, self.root = workspace("| A | Robin | open | 09:00 |  | c |", "| A | none |")
        self.addCleanup(self.tmp.cleanup)
        self.main = git("rev-parse", "--short", "main", cwd=self.root / "repo")

    def test_it_names_mains_commit_and_not_the_worktrees(self):
        tree = self.root / "trees" / "wt-unmerged"
        self.assertNotEqual(git("rev-parse", "--short", "HEAD", cwd=tree), self.main)
        self.assertIn(f"main {self.main}", al._push_gap(tree))

    def test_the_count_and_the_sha_describe_the_same_branch(self):
        """The count resolved `main` while the sha resolved `HEAD`, so a right number vouched for a wrong name."""
        line = al._push_gap(self.root / "trees" / "wt-unmerged")
        self.assertIn(f"main {self.main}", line)
        self.assertIn("even with origin/main", line)


class RefJoinTest(unittest.TestCase):
    """Finding 112. A ref is minted once and survives a rename; a name is a tab title, and one changed
    at 03:21 on Zach's word that the worktree is the name. The join read the names, missed, and called
    a session that had committed five minutes earlier orphaned. Where both sides carry a ref, the ref
    is the join.
    """

    DIRTY = "| Unsaved work | sam | waiting | 09:00 |  | Checklist: Unsaved work |"

    def audit(self, ownership: str, sessions: str):
        tmp, root = workspace(self.DIRTY, ownership, sessions=sessions)
        self.addCleanup(tmp.cleanup)
        return al.audit(root, "2026-09-17")

    def test_a_renamed_session_is_not_an_orphan(self) -> None:
        report = self.audit("| sam [768194] | worktree wt-dirty (feat/dirty) |",
                            session_row("**openemr-arch** (tab title `sam`)", ref="768194"))
        self.assertEqual(report.orphans, [], report.lines)

    def test_a_bolded_session_name_still_joins_when_there_is_no_ref(self) -> None:
        report = self.audit("| sam | worktree wt-dirty (feat/dirty) |", session_row("**sam**"))
        self.assertEqual(report.orphans, [], report.lines)

    def test_a_ref_no_session_carries_is_an_orphan_however_familiar_the_name(self) -> None:
        report = self.audit("| sam [999999] | worktree wt-dirty (feat/dirty) |",
                            session_row("sam", ref="a1b2c3"))
        self.assertEqual(len(report.orphans), 1, report.lines)
        self.assertIn("999999", str(report.orphans[0]))

    def test_with_no_ref_on_the_owner_the_name_still_decides(self) -> None:
        report = self.audit("| sam | worktree wt-dirty (feat/dirty) |", session_row("sam", ref="768194"))
        self.assertEqual(report.orphans, [], report.lines)

    def test_a_tracker_whose_sessions_carry_no_refs_falls_back_to_names(self) -> None:
        """An empty ref column is not a roster of nobody, the same way an empty table is not."""
        report = self.audit("| sam [768194] | worktree wt-dirty (feat/dirty) |", session_row("sam", ref=""))
        self.assertEqual(report.orphans, [], report.lines)


class OwnsRefTest(unittest.TestCase):
    """Finding 115. 112 put the ref in the roster join and stopped there. `owns()` — the join that
    attributes a task to an ownership row, and so to a tree — still read names, and today's tracker
    runs one ref under three names: `arch [768194]`, `architecture-review-setup [768194]`,
    `openemr-arch [768194]`. Four tasks matched no row, so no tree, so no reopen, orphan or stop
    check reached them. Silently unaudited is worse than wrongly audited.
    """

    def row(self, context: str) -> al.OwnerRow:
        return al.parse_ownership(f"| {context} | worktree wt-a (feat/a) |")[0]

    def test_two_names_on_one_ref_are_one_context(self) -> None:
        self.assertTrue(al.owns(self.row("architecture-review-setup [768194]"), "openemr-arch [768194]"))

    def test_one_name_on_two_refs_is_two_contexts(self) -> None:
        self.assertFalse(al.owns(self.row("sam [aaaaaa]"), "sam [bbbbbb]"))

    def test_with_a_ref_on_one_side_only_the_name_still_decides(self) -> None:
        self.assertTrue(al.owns(self.row("sam"), "sam [aaaaaa]"))
        self.assertTrue(al.owns(self.row("sam [aaaaaa]"), "sam"))

    def test_a_task_renamed_away_from_its_row_is_still_audited(self) -> None:
        tmp, root = workspace(
            "| Landed work | openemr-arch [768194] | done | 09:00 |  | Checklist: Landed work |",
            "| architecture-review-setup [768194] | worktree wt-unmerged (feat/open) |",
            sessions=session_row("openemr-arch", ref="768194"))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(len(report.reopen), 1, report.lines)
        self.assertIn("wt-unmerged", str(report.reopen[0]))


class ReopenGroupsByTreeTest(unittest.TestCase):
    """Finding 116, option (c), from `gauntlet-b2` by way of `board-ux-improvements`.

    `audit_tasks` asks git once per tree and then fans that one fact across the tree's tasks, so
    thirteen reopen lines on the live tracker carried three facts. Grouping the *rendering* per tree
    is not a change to what is detected: every task that reopened still appears, the finding list is
    untouched, and the exit code is unchanged. It is the ratio that moves, and the ratio is lines per
    tree, which is what made the count climb as a session closed more tasks in one worktree.
    """

    TWO = ("| First | robin | done 10:00 | 09:00 |  | Checklist: First |\n"
           "| Second | robin | done 10:00 | 09:00 |  | Checklist: Second |")
    OWNS = "| robin | worktree wt-unmerged (feat/open) |"

    def report(self):
        tmp, root = workspace(self.TWO, self.OWNS, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        return al.audit(root, "2026-09-17")

    def test_two_tasks_on_one_tree_render_as_one_line(self) -> None:
        lines = [l for l in self.report().lines if l.startswith("reopen:")]
        self.assertEqual(len(lines), 1, lines)

    def test_and_that_line_names_every_task_it_covers(self) -> None:
        """The invariant that makes this safe: grouping may not drop a task from the output."""
        report = self.report()
        rendered = "\n".join(l for l in report.lines if l.startswith("reopen:"))
        for r in report.reopen:
            self.assertIn(short_name(r.task), rendered)

    def test_the_finding_list_and_the_exit_code_are_untouched(self) -> None:
        report = self.report()
        self.assertEqual(sorted(r.task for r in report.reopen), ["First", "Second"])

    def test_the_summary_says_how_many_facts_and_how_many_tasks(self) -> None:
        summary = self.report().lines[-1]
        self.assertIn("reopen=1 tree", summary)
        self.assertIn("2 task", summary)

    def test_two_trees_stay_two_lines(self) -> None:
        tmp, root = workspace(
            "| First | robin | done 10:00 | 09:00 |  | Checklist: First |\n"
            "| Other | sam | done 10:00 | 09:00 |  | Checklist: Other |",
            "| robin | worktree wt-unmerged (feat/open) |\n| sam | worktree wt-dirty (feat/dirty) |",
            sessions=session_row("robin") + "\n" + session_row("sam"))
        self.addCleanup(tmp.cleanup)
        lines = [l for l in al.audit(root, "2026-09-17").lines if l.startswith("reopen:")]
        self.assertEqual(len(lines), 2, lines)

    def test_a_bolded_task_item_is_unmarked_before_it_is_shortened(self) -> None:
        """`short_name` cutting inside a `**` pair returns a bare ellipsis — the same defect fixed for
        the decision queue and never carried across. Grouping only made it visible: six tasks on one
        line rendered as six ellipses, and the old per-task output had been doing it all along."""
        tmp, root = workspace(
            "| **A task whose item is bold and much longer than the short_name cut** | robin "
            "| done 10:00 | 09:00 |  | Checklist: x |",
            self.OWNS, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        line = [l for l in al.audit(root, "2026-09-17").lines if l.startswith("reopen:")][0]
        self.assertIn("A task whose item is bold", line)
        self.assertNotIn("**", line)


class ShaClosesTheLaneTest(unittest.TestCase):
    """Finding 116(a). `reopen` asks whether the owner's *tree* is on main; `done` claims that *this
    task's change* reached main. A session working continuously in one worktree always has unmerged
    work, so every task it ever closed reopened — six of arch's did on the live tracker, and every
    one of their changes was already on main.

    A `done` state may name the commit that carried it: `done 21:16–22:05 54b7eb3`. Only positive
    proof clears a task. Absent, malformed, unknown to git, or git itself failing all fall back to
    exactly today's behaviour, which is noisy, self-clearing and visible — the property 116a's time
    heuristic did not have, and why it was discarded after this suite falsified it.
    """

    OWNS = "| robin | worktree wt-unmerged (feat/open) |"
    DIRTY = "| sam | worktree wt-dirty (feat/dirty) |"

    def tracker(self, state: str, ownership: str | None = None, sessions: str = "", owner: str = "robin"):
        tmp, root = workspace(f"| Landed work | {owner} | @STATE@ | 09:00 |  | Checklist: Landed work |",
                              ownership or self.OWNS, sessions)
        self.addCleanup(tmp.cleanup)
        repo = root / "repo"
        state = state.replace("@main@", git("rev-parse", "--short", "main", cwd=repo).strip())
        state = state.replace("@open@", git("rev-parse", "--short", "feat/open", cwd=repo).strip())
        p = root / "daily" / "2026-09-17-tracker.md"
        p.write_text(p.read_text().replace("@STATE@", state))
        return al.audit(root, "2026-09-17")

    def test_a_sha_on_main_closes_the_task_though_its_tree_is_not(self) -> None:
        """The whole point: the tree's other unmerged work is not evidence about this task."""
        report = self.tracker("done 10:00 @main@")
        self.assertEqual(report.reopen, [], report.lines)

    def test_a_sha_not_on_main_reopens_and_the_line_names_it(self) -> None:
        report = self.tracker("done 10:00 @open@")
        self.assertEqual(len(report.reopen), 1, report.lines)
        self.assertIn("is not on main", report.reopen[0].why)

    def test_a_task_with_no_sha_reopens_exactly_as_before(self) -> None:
        """The regression guard against 116a: no sha is no evidence, and no evidence clears nothing."""
        report = self.tracker("done 10:00")
        self.assertEqual(len(report.reopen), 1, report.lines)
        self.assertIn("not on main", report.reopen[0].why)

    def test_a_malformed_sha_falls_back_and_says_so(self) -> None:
        report = self.tracker("done 10:00 not-a-sha")
        self.assertEqual(len(report.reopen), 1, report.lines)
        self.assertTrue(any("not-a-sha" in l for l in report.lines), report.lines)

    def test_a_sha_no_repo_knows_falls_back_and_says_so(self) -> None:
        """Stale evidence is not proof. A sha nothing can resolve must never clear a task."""
        report = self.tracker("done 10:00 deadbee")
        self.assertEqual(len(report.reopen), 1, report.lines)
        self.assertTrue(any("deadbee" in l for l in report.lines), report.lines)

    def test_a_closed_task_is_still_checked_for_orphaned_work(self) -> None:
        """This rule touches `reopen` and nothing else."""
        # `sam`, not the fixture's user: the user is exempt from the orphan join by design.
        report = self.tracker("done 10:00 @main@", ownership=self.DIRTY,
                              sessions=session_row("kim"), owner="sam")
        self.assertEqual(report.reopen, [], report.lines)
        self.assertEqual(len(report.orphans), 1, report.lines)


# Zach, 2026-09-22 22:20: each task "is actually backed by an entry in github". The audit is where the
# issue's state is read; `gh` is injected, so nothing here touches the network.
ISSUE_CLAUDE = CLAUDE + "- Backlog: GitHub issues; repo https://github.com/o/backlog (private)\n"

ISSUE_TRACKER = """# Tracker 2026-09-17

## Tasks

| name | item | owner | state | since | due | size | issue | checklist |
|---|---|---|---|---|---|---|---|---|
| Bare | Bare task | robin | open | 09:00 |  | S |  | c |
| Garbled | Garbled task | robin | open | 09:00 |  | S | TBD | c |
| Closed under it | Task whose issue closed | robin | waiting | 09:00 |  | S | #5 | c |
| Missing | Task whose issue is missing | robin | running 09:10 | 09:00 |  | S | #6 | c |
| Done open | Finished, issue still open | robin | done 09:00–10:00 | 09:00 |  | S | #7 | c |
| Backed | Backed task | robin | open | 09:00 |  | S | #8 | c |
| Old done | Closed before the rule | robin | done 08:00 | 08:00 |  | S |  | c |
| Elsewhere | Task in another repo | robin | open | 09:00 |  | S | x/y#2 | c |
| impl-3 — standing implementer | impl-3: standing implementer; wait idle | impl-3 | open | 09:00 |  |  |  |  |

## Log

- 09:00 opened the day
"""


class FakeGh:
    """`gh issue list --state all -R <repo>`, answered per repo. Records argv."""

    def __init__(self, repos: dict | None = None, fail: tuple | None = None):
        self.repos = repos or {}
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(args))
        assert args[:2] == ["issue", "list"], args
        if self.fail:
            return self.fail
        repo = args[args.index("-R") + 1]
        rows = [{"number": n, "title": "t", "state": st, "labels": [], "url": "", "updatedAt": ""}
                for n, st in self.repos.get(repo, {}).items()]
        return 0, json.dumps(rows), ""


LIVE = {"o/backlog": {5: "CLOSED", 7: "OPEN", 8: "OPEN"}, "x/y": {2: "OPEN"}}


def issue_workspace(claude: str = ISSUE_CLAUDE) -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "daily").mkdir()
    (root / "CLAUDE.md").write_text(claude)
    (root / "daily" / "2026-09-17-tracker.md").write_text(ISSUE_TRACKER)
    return tmp, root


# Zach, 2026-09-23 22:40: "remove the github issue remote and make everything use gitlab now that we have that going."
GITLAB_CLAUDE = CLAUDE + "- Backlog: GitLab; host https://gl.example; project o/backlog\n"


class GitLabIssueAuditTest(unittest.TestCase):
    def test_a_gitlab_backlog_is_audited(self):
        import backlog as bl
        tmp, root = issue_workspace(GITLAB_CLAUDE)
        self.addCleanup(tmp.cleanup)
        real = bl.issues

        def fake(cfg, state="opened", **kw):
            if isinstance(cfg, bl.Backlog):
                self.assertEqual((cfg.host, cfg.project, state), ("https://gl.example", "o/backlog", "all"))
                return bl.Fetch(issues=tuple(bl.Issue(n, "t", bl.CLOSED if s == "CLOSED" else bl.OPEN)
                                             for n, s in LIVE["o/backlog"].items()))
            return real(cfg, state=state, **kw)
        with patch.object(bl, "issues", fake):
            report = al.audit(root, "2026-09-17", gh=FakeGh(LIVE))
        self.assertEqual([str(f) for f in report.issues], [
            "issue: Bare — no issue; file it with backlog.py --create",
            'issue: Garbled — "TBD" is not an issue reference',
            "issue: Closed under it — #5 is closed",
            "issue: Missing — #6 not found in o/backlog",
            "issue: Done open — done but #7 is open; backlog.py --close 7 --commit",
        ])


class ForeignHostTest(unittest.TestCase):
    def test_a_cell_on_another_host_is_never_read(self):
        # R1 on PR 14: an issue cell of https://evil.example.com/a/b/-/issues/1 sent the Backlog's token to that host.
        import backlog as bl
        tmp, root = issue_workspace(GITLAB_CLAUDE)
        self.addCleanup(tmp.cleanup)
        tracker = root / "daily" / "2026-09-17-tracker.md"
        tracker.write_text(tracker.read_text().replace("| x/y#2 |", "| https://evil.example.com/a/b/-/issues/1 |"))
        asked = []

        def fake(cfg, state="opened", **_kw):
            asked.append(getattr(cfg, "host", "github"))
            return bl.Fetch(issues=())
        with patch.object(bl, "issues", fake):
            report = al.audit(root, "2026-09-17", gh=FakeGh(LIVE))
        self.assertNotIn("https://evil.example.com", asked)
        self.assertIn('issue: Elsewhere — "https://evil.example.com/a/b/-/issues/1" is not an issue reference',
                      [str(f) for f in report.issues])


class SameHostProjectTest(unittest.TestCase):
    def test_another_project_on_the_backlog_host_is_read_on_that_host(self):
        import backlog as bl
        tmp, root = issue_workspace(GITLAB_CLAUDE)
        self.addCleanup(tmp.cleanup)
        tracker = root / "daily" / "2026-09-17-tracker.md"
        tracker.write_text(tracker.read_text().replace("| x/y#2 |", "| https://gl.example/g/other/-/issues/2 |"))
        asked = []

        def fake(cfg, state="opened", **_kw):
            asked.append((type(cfg).__name__, getattr(cfg, "host", ""), getattr(cfg, "project", "")))
            return bl.Fetch(issues=(bl.Issue(2, "t", bl.OPEN),))
        with patch.object(bl, "issues", fake):
            al.audit(root, "2026-09-17", gh=FakeGh(LIVE))
        self.assertIn(("Backlog", "https://gl.example", "g/other"), asked)


class IssueAuditTest(unittest.TestCase):
    def setUp(self):
        tmp, self.root = issue_workspace()
        self.addCleanup(tmp.cleanup)

    def run_audit(self, gh, **kw):
        return al.audit(self.root, "2026-09-17", gh=gh, **kw)

    def test_each_finding(self):
        report = self.run_audit(FakeGh(LIVE))
        got = [str(f) for f in report.issues]
        self.assertEqual(got, [
            "issue: Bare — no issue; file it with backlog.py --create",
            'issue: Garbled — "TBD" is not an issue reference',
            "issue: Closed under it — #5 is closed",
            "issue: Missing — #6 not found in o/backlog",
            "issue: Done open — done but #7 is open; backlog.py --close 7 --commit",
        ])
        for line in got:
            self.assertIn(line, report.lines)
        self.assertIn("issues=5", report.lines[-1])

    def test_one_call_per_repo(self):
        gh = FakeGh(LIVE)
        self.run_audit(gh)
        self.assertEqual(sorted(c[c.index("-R") + 1] for c in gh.calls), ["o/backlog", "x/y"])
        for call in gh.calls:
            self.assertEqual(call[call.index("--state") + 1], "all")

    def test_unknown_is_a_finding_never_a_pass(self):
        report = self.run_audit(FakeGh(fail=(1, "", "rate limited")))
        unknown = [str(f) for f in report.issues if str(f).startswith("issue: unknown — ")]
        self.assertEqual(len(unknown), 1, report.lines)
        self.assertIn("rate limited", unknown[0])
        self.assertNotIn("issues=0", report.lines[-1])

    def test_off(self):
        gh = FakeGh(LIVE)
        report = self.run_audit(gh, check_issues=False)
        self.assertEqual((gh.calls, report.issues), ([], []))
        self.assertIn("issues=off", report.lines[-1])

    def test_no_backlog_line_is_no_check(self):
        tmp, root = issue_workspace(CLAUDE)
        self.addCleanup(tmp.cleanup)
        gh = FakeGh(LIVE)
        report = al.audit(root, "2026-09-17", gh=gh)
        self.assertEqual((gh.calls, report.issues), ([], []))
        self.assertNotIn("issues=", report.lines[-1])

    def test_exit_code_counts_issue_findings(self):
        self.assertEqual(al.main(["--root", str(self.root), "--date", "2026-09-17"], gh=FakeGh(LIVE)), 5)

    def test_cli_off(self):
        gh = FakeGh(LIVE)
        self.assertEqual(al.main(["--root", str(self.root), "--date", "2026-09-17", "--no-issues"], gh=gh), 0)
        self.assertEqual(gh.calls, [])


LANE_CLAUDE = CLAUDE + "- Settings: `cos.toml`\n"
LANE_TOML = '[lanes]\nbuild = { stages = ["implement", "pr", "review", "triage", "merge"], gates = ["triage", "merge"] }\n'
LANE_TRACKER = TRACKER.replace(
    "| item | owner | state | since | due | checklist |\n|---|---|---|---|---|---|\n{rows}",
    "| name | item | owner | state | since | due | size | lane | stage | issue | checklist |\n|---|---|---|---|---|---|---|---|---|---|---|\n"
    "| Good | Good one | a | running 10:00 | 10:00 | | M | build | review | | c |\n"
    "| Unnamed lane | x | a | open | 10:00 | | M | designed | design | | c |\n"
    "| Wrong stage | x | a | open | 10:00 | | M | build | design | | c |\n"
    "| Blank stage | x | a | open | 10:00 | | M | build | | | c |\n"
    "| No lane | a console action | a | open | 10:00 | | S | | | | c |").format(rows="", ownership="", sessions="")


class LaneAuditTest(unittest.TestCase):
    """#33: a task's `lane` is one the toml names, and its `stage` is a stage of that lane."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "daily").mkdir()
        (self.root / "CLAUDE.md").write_text(LANE_CLAUDE)
        (self.root / "cos.toml").write_text(LANE_TOML)
        (self.root / "daily" / "2026-09-17-tracker.md").write_text(LANE_TRACKER)

    def test_each_finding(self):
        report = al.audit(self.root, "2026-09-17")
        got = [str(f) for f in report.lanes]
        self.assertEqual(got, [
            "lane: Unnamed lane — lane designed is not in cos.toml",
            "lane: Wrong stage — stage design is not a stage of build",
            "lane: Blank stage — lane build but no stage",
        ])
        for line in got:
            self.assertIn(line, report.lines)
        self.assertIn(" lanes=3", report.lines[-1])

    def test_the_exit_counts_them(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(al.main(["--root", str(self.root), "--date", "2026-09-17"]), 3)

    def test_no_lane_column_says_nothing(self):
        (self.root / "daily" / "2026-09-17-tracker.md").write_text(TRACKER.format(rows="| x | a | open | 10:00 | | c |", ownership="", sessions=""))
        report = al.audit(self.root, "2026-09-17")
        self.assertEqual(report.lanes, [])
        self.assertNotIn("lanes=", report.lines[-1])


BUDGET_TOML = '[budgets]\nS = "30m"\nM = "90m"\nL = "3h"\n'
BUDGET_TRACKER = TRACKER.replace(
    "| item | owner | state | since | due | checklist |\n|---|---|---|---|---|---|\n{rows}",
    "| name | item | owner | state | since | due | size | checklist |\n|---|---|---|---|---|---|---|---|\n"
    "| Long M | x | a | running 10:00 | 10:00 | | M | c |\n"
    "| Short M | x | a | running 11:00 | 11:00 | | M | c |\n"
    "| Long S | x | a | running 11:30 | 11:30 | | S | c |\n"
    "| At S | x | a | running 11:40 | 11:40 | | S | c |\n"
    "| Huge XL | x | a | running 06:00 | 06:00 | | XL | c |\n"
    "| Unsized | x | a | running 06:00 | 06:00 | | | c |\n"
    "| Open L | x | a | open | 06:00 | | L | c |").format(rows="", ownership="", sessions="")


class BudgetAuditTest(unittest.TestCase):
    """#33 stage 3: a running task past its size's budget is named; the coordinator polls it and never kills it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "daily").mkdir()
        (self.root / "CLAUDE.md").write_text(LANE_CLAUDE)
        (self.root / "cos.toml").write_text(BUDGET_TOML)
        (self.root / "daily" / "2026-09-17-tracker.md").write_text(BUDGET_TRACKER)

    def test_over_and_under(self):
        now = datetime(2026, 9, 17, 12, 10, tzinfo=ZoneInfo("America/Chicago"))
        report = al.audit(self.root, "2026-09-17", now=now)
        got = [str(o) for o in report.over]
        self.assertEqual(got, ["over budget: Long M running 2h10m (M 1h30m)", "over budget: Long S running 40m (S 30m)"])
        for line in got:
            self.assertIn(line, report.lines)
        self.assertIn(" over=2", report.lines[-1])

    def test_no_budgets_says_nothing(self):
        (self.root / "cos.toml").write_text("")
        report = al.audit(self.root, "2026-09-17", now=datetime(2026, 9, 17, 23, 0, tzinfo=ZoneInfo("America/Chicago")))
        self.assertEqual(report.over, [])
        self.assertNotIn("over=", report.lines[-1])
