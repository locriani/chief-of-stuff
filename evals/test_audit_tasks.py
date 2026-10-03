"""Unit tests for scripts/audit_tasks.py: a task is done when its change is on main, and a script says so, not a grep.

Repos here are real: a bare origin, a clone that has fetched it, and worktrees off that clone.
`git merge-base --is-ancestor` and `git status --porcelain` have no useful fake, and the point of the
script is that its answer is git's answer.
"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_tasks as al  # noqa: E402
import git_trees  # noqa: E402
import board_sources as bs  # noqa: E402
import test_board_sources as tbs  # noqa: E402  (module-qualified: don't re-collect its TestCases)
from tracker import short_name  # noqa: E402

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

    def test_a_plain_word_after_worktree_is_not_a_tree(self):
        """A tree name is backticked or shaped like one; `preserved` was reported as a tree never created (#72)."""
        rows = al.parse_ownership("| impl-x | `src/x.py`; worktree preserved for review |")
        self.assertEqual(rows[0].worktrees, [])

    def test_a_backticked_plain_word_is_a_tree(self):
        self.assertEqual(al.parse_ownership("| a | worktree `demo` |")[0].worktrees, ["demo"])

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

    OPEN = "| Open branch work | sam | waiting | 09:00 |  | Checklist: Open branch work |"
    OPEN_OWNS = "| sam | worktree wt-unmerged (feat/open) |"

    def test_pushed_work_is_not_an_orphan_however_gone_its_owner(self) -> None:
        """An unmerged branch a remote holds is recoverable by name. One only this tree holds is not (#43)."""
        tmp, root = workspace(self.OPEN, self.OPEN_OWNS, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        git("push", "-u", "origin", "feat/open", cwd=root / "trees" / "wt-unmerged")
        self.assertEqual(al.audit(root, "2026-09-17").orphans, [])

    def test_unpushed_work_is_an_orphan_when_its_owner_is_gone(self) -> None:
        """#43: a commit that existed only locally, with no upstream, and the audit said nothing."""
        tmp, root = workspace(self.OPEN, self.OPEN_OWNS, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(len(report.orphans), 1, report.lines)
        line = str(report.orphans[0])
        self.assertIn("wt-unmerged", line)
        self.assertIn("1 not on origin/main", line)
        self.assertIn("no upstream", line)

    def test_a_commit_ahead_of_its_upstream_is_counted(self) -> None:
        tmp, root = workspace(self.OPEN, self.OPEN_OWNS, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        tree = root / "trees" / "wt-unmerged"
        git("push", "-u", "origin", "feat/open", cwd=tree)
        (tree / "more.txt").write_text("more\n")
        git("add", "more.txt", cwd=tree)
        git("commit", "-m", "more", cwd=tree)
        [orphan] = al.audit(root, "2026-09-17").orphans
        self.assertIn("1 unpushed", str(orphan))

    # A one-shot's File ownership row is keyed on its task, which is never a session.
    ONE_SHOT = "| Unsaved work | impl-01 | running 09:10 | 09:00 |  | Checklist: Unsaved work |"
    TASK_KEYED = "| Unsaved work | worktree wt-dirty (feat/dirty) |"

    def launcher(self, root: Path, pid: int) -> None:
        d = root / "trees/wt-dirty/.chief-of-stuff"
        d.mkdir(exist_ok=True)
        (d / ".gitignore").write_text("*\n")
        (d / "one-shot.pid").write_text(f"{pid} Unsaved work\n")

    def test_a_running_one_shots_tree_is_not_an_orphan(self) -> None:
        tmp, root = workspace(self.ONE_SHOT, self.TASK_KEYED, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        self.launcher(root, os.getpid())
        self.assertEqual(al.audit(root, "2026-09-17").orphans, [])

    def test_a_one_shot_handed_to_the_user_is_not_an_orphan(self) -> None:
        # After a review hold the task is the user's, and its tree waits for them.
        tmp, root = workspace(self.ONE_SHOT.replace("impl-01 | running 09:10", "Robin | waiting"), self.TASK_KEYED,
                              sessions=session_row("sam"))
        self.addCleanup(tmp.cleanup)
        self.assertEqual(al.audit(root, "2026-09-17").orphans, [])

    def test_a_one_shot_whose_launcher_died_is_still_an_orphan(self) -> None:
        tmp, root = workspace(self.ONE_SHOT, self.TASK_KEYED, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        gone = subprocess.Popen([sys.executable, "-c", ""])
        gone.wait()
        self.launcher(root, gone.pid)
        self.assertEqual(len(al.audit(root, "2026-09-17").orphans), 1)

    def test_the_count_reaches_the_summary_line(self) -> None:
        tmp, root = workspace(self.DIRTY, self.OWNS, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        self.assertIn("orphaned=1", al.audit(root, "2026-09-17").lines[-1])


class TreesOnDiskTest(unittest.TestCase):
    """#43. `visit()` reached only trees a File ownership row named, so a gone session's tree that no
    row named was never looked at, and the audit printed `trees=0` over two trees with work at risk.
    The trees on disk are the entry point now; rows, registrations and launchers only say whose they are.
    """

    TASK = "| Landed work | robin | done 10:00 | 09:00 |  | Checklist: Landed work |"

    def register(self, root: Path, name: str, pid: int, tree: str) -> None:
        folder = root / ".chief-of-stuff" / "sessions"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name}.json").write_text(json.dumps(
            {"pid": pid, "name": name, "runtime": "codex", "worktree": str(root / "trees" / tree)}))

    def dead_pid(self) -> int:
        proc = subprocess.Popen([sys.executable, "-c", ""])
        proc.wait()
        return proc.pid

    def test_a_gone_workers_unpushed_tree_no_row_names_is_orphaned(self) -> None:
        """The acceptance case: one committed, unpushed commit; no File ownership row; its worker's PID gone."""
        tmp, root = workspace(self.TASK, "", sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        self.register(root, "impl-7", self.dead_pid(), "wt-unmerged")
        report = al.audit(root, "2026-09-17")
        [orphan] = [o for o in report.orphans if "wt-unmerged" in str(o)]
        self.assertIn("1 not on origin/main", str(orphan))
        self.assertIn("impl-7", str(orphan))
        self.assertIn("no upstream", str(orphan))

    def test_a_live_workers_tree_is_not_orphaned(self) -> None:
        tmp, root = workspace(self.TASK, "", sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        self.register(root, "impl-7", os.getpid(), "wt-unmerged")
        report = al.audit(root, "2026-09-17")
        self.assertFalse(any("wt-unmerged" in str(o) for o in report.orphans + report.unclaimed), report.lines)

    def test_at_risk_work_nothing_claims_is_unclaimed(self) -> None:
        tmp, root = workspace(self.TASK, "", sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(report.orphans, [], report.lines)
        self.assertEqual(sorted(str(u).split(" — ")[0] for u in report.unclaimed),
                         ["unclaimed work: wt-dirty (feat/dirty)", "unclaimed work: wt-unmerged (feat/open)"])
        self.assertIn(" unclaimed=2", report.lines[-1])

    def test_unclaimed_needs_no_sessions_table(self) -> None:
        """No roster means no claim that an owner is gone; a tree nobody names needs no owner to be at risk."""
        tmp, root = workspace(self.TASK, "")
        self.addCleanup(tmp.cleanup)
        self.assertEqual(len(al.audit(root, "2026-09-17").unclaimed), 2)

    def test_every_tree_on_disk_is_counted(self) -> None:
        tmp, root = workspace(self.TASK, "", sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertIn(" trees=3 ", report.lines[-1])
        self.assertTrue(any(ln.startswith("wt-merged (landed): ") for ln in report.lines), report.lines)

    def test_safe_trees_raise_nothing(self) -> None:
        tmp, root = workspace(self.TASK, "", sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertFalse(any("wt-merged" in str(f) for f in report.orphans + report.unclaimed), report.lines)

    def test_no_unclaimed_count_when_there_is_none(self) -> None:
        tmp, root = workspace(self.TASK, "| robin | worktree wt-dirty · worktree wt-unmerged |", sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        self.assertNotIn("unclaimed=", al.audit(root, "2026-09-17").lines[-1])


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
        # The audit walks the trees on disk (#43), so none may be left there.
        for name in ("wt-merged", "wt-unmerged", "wt-dirty"):
            git("worktree", "remove", "--force", str(root / "trees" / name), cwd=root / "repo")
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
    """`gh issue list --state all -R <repo>`, answered per repo. Records argv.

    `closed_at`, keyed the same way as `repos` (repo -> {number: iso timestamp}), answers #221's
    `closedAt`; a number with no entry there answers null, `gh`'s own word for an issue that never
    closed.
    """

    def __init__(self, repos: dict | None = None, fail: tuple | None = None, closed_at: dict | None = None):
        self.repos = repos or {}
        self.fail = fail
        self.closed_at = closed_at or {}
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(args))
        assert args[:2] == ["issue", "list"], args
        if self.fail:
            return self.fail
        repo = args[args.index("-R") + 1]
        rows = [{"number": n, "title": "t", "state": st, "labels": [], "url": "", "updatedAt": "",
                 "closedAt": self.closed_at.get(repo, {}).get(n)}
                for n, st in self.repos.get(repo, {}).items()]
        return 0, json.dumps(rows), ""


LIVE = {"o/backlog": {5: "CLOSED", 7: "OPEN", 8: "OPEN"}, "x/y": {2: "OPEN"}}


def issue_workspace(claude: str = ISSUE_CLAUDE, tracker: str = ISSUE_TRACKER) -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "daily").mkdir()
    (root / "CLAUDE.md").write_text(claude)
    (root / "daily" / "2026-09-17-tracker.md").write_text(tracker)
    return tmp, root


# Zach, 2026-09-23 22:40: "remove the github issue remote and make everything use gitlab now that we have that going."
GITLAB_CLAUDE = CLAUDE + "- Backlog: GitLab; host https://gl.example; project o/backlog\n"


def gitlab_issues(case: unittest.TestCase):
    """`backlog.issues` patched so the GitLab Backlog's list is LIVE's `o/backlog`; a GitHub repo is still `gh`'s."""
    import backlog as bl
    real = bl.issues

    def fake(cfg, state="opened", **kw):
        if isinstance(cfg, bl.Backlog):
            case.assertEqual((cfg.host, cfg.project, state), ("https://gl.example", "o/backlog", "all"))
            return bl.Fetch(issues=tuple(bl.Issue(n, "t", bl.CLOSED if s == "CLOSED" else bl.OPEN)
                                         for n, s in LIVE["o/backlog"].items()))
        return real(cfg, state=state, **kw)
    return patch.object(bl, "issues", fake)


class GitLabIssueAuditTest(unittest.TestCase):
    def test_a_gitlab_backlog_is_audited(self):
        tmp, root = issue_workspace(GITLAB_CLAUDE)
        self.addCleanup(tmp.cleanup)
        with gitlab_issues(self):
            report = al.audit(root, "2026-09-17", gh=FakeGh(LIVE))
        self.assertEqual([str(f) for f in report.issues], [
            "issue: Bare — no issue; file it with chief-of-stuff backlog --create",
            'issue: Garbled — "TBD" is not an issue reference',
            "issue: Closed under it — #5 is closed; write the task done",
            "issue: Missing — #6 not found in o/backlog",
            "issue: Done open — done but #7 is open; chief-of-stuff backlog --close 7 --commit",
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
            "issue: Bare — no issue; file it with chief-of-stuff backlog --create",
            'issue: Garbled — "TBD" is not an issue reference',
            "issue: Closed under it — #5 is closed; write the task done",
            "issue: Missing — #6 not found in o/backlog",
            "issue: Done open — done but #7 is open; chief-of-stuff backlog --close 7 --commit",
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


class IssueClosesAtLaneEndTest(unittest.TestCase):
    """A done row mid-lane is one stage of its issue's work; the issue closes when its card reaches the lane's end."""

    def test_only_a_row_done_at_the_last_stage_asks_to_close(self):
        tmp, root = issue_workspace(ISSUE_CLAUDE + "- Settings: `cos.toml`\n")
        self.addCleanup(tmp.cleanup)
        (root / "cos.toml").write_text(LANE_TOML)
        (root / "daily" / "2026-09-17-tracker.md").write_text(
            "# Tracker 2026-09-17\n\n## Tasks\n\n"
            "| name | item | owner | state | since | due | size | lane | stage | issue | checklist |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|\n"
            "| Review done | Review the change | a | done 09:00–10:00 | 09:00 | | S | build | review | #7 | c |\n"
            "| Merged | Merge the change | a | done 10:00–10:30 | 10:00 | | S | build | merge | #8 | c |\n\n## Log\n")
        report = al.audit(root, "2026-09-17", gh=FakeGh(LIVE))
        self.assertEqual([str(f) for f in report.issues],
                         ["issue: Merged — done but #8 is open; chief-of-stuff backlog --close 8 --commit"])


class ClosedIssueOnOpenTaskTest(unittest.TestCase):
    """#221: an issue closed on the forge (by a person, or a change's closing keyword) while its tracker
    task is still open/waiting/orphaned never moved the task before this — the audit only said
    "#N is closed" and stopped. The fault now carries its own fix, the way a done-but-open row already
    names `chief-of-stuff backlog --close N --commit`: write the task done when nothing is left to land,
    or hand the call to the user when the tree still holds work `main` never got.
    """

    TRACKER = """# Tracker 2026-09-17

## Tasks

| name | item | owner | state | since | due | size | issue | checklist |
|---|---|---|---|---|---|---|---|---|
| No tree work | No tree work, backed by an issue with no worktree | sam | waiting | 09:00 |  | S | #5 | c |
| Unlanded work | Unlanded work, its tree still ahead of main | robin | waiting | 09:00 |  | S | #6 | c |
| Landed work | Landed work, its tree already merged | kim | waiting | 09:00 |  | S | #7 | c |

## File ownership

| context | paths |
|---|---|
| robin | worktree wt-unmerged (feat/open) |
| kim | worktree wt-merged (landed) |

## Log

- 09:00 opened the day
"""

    def workspace(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "daily").mkdir()
        (root / "CLAUDE.md").write_text(ISSUE_CLAUDE)
        (root / "daily" / "2026-09-17-tracker.md").write_text(self.TRACKER)
        build(root)
        self.addCleanup(tmp.cleanup)
        return root

    def test_no_worktree_writes_the_task_done_at_the_close_time(self):
        """Branch A, no tree at all: nothing can be unlanded, so the fix is to write the task done —
        at the time the forge closed the issue, converted into the workspace's own zone."""
        gh = FakeGh({"o/backlog": {5: "CLOSED", 6: "CLOSED", 7: "CLOSED"}},
                    closed_at={"o/backlog": {5: "2026-09-17T19:05:00Z"}})
        report = al.audit(self.workspace(), "2026-09-17", gh=gh)
        self.assertIn("issue: No tree work — #5 is closed 14:05; write the task done at 14:05",
                      [str(f) for f in report.issues])

    def test_a_landed_worktree_also_writes_the_task_done(self):
        """Branch A, a tree that IS there: `wt-merged`'s branch is already an ancestor of main, so it
        holds no work `main` is missing either, and the fix is the same as having no tree at all."""
        gh = FakeGh({"o/backlog": {5: "CLOSED", 6: "CLOSED", 7: "CLOSED"}},
                    closed_at={"o/backlog": {7: "2026-09-17T19:05:00Z"}})
        report = al.audit(self.workspace(), "2026-09-17", gh=gh)
        self.assertIn("issue: Landed work — #7 is closed 14:05; write the task done at 14:05",
                      [str(f) for f in report.issues])

    def test_no_time_is_named_when_the_forge_gave_none(self):
        """An empty `closed_at` omits the clock rather than printing a false one."""
        gh = FakeGh({"o/backlog": {5: "CLOSED", 6: "CLOSED", 7: "CLOSED"}})
        report = al.audit(self.workspace(), "2026-09-17", gh=gh)
        self.assertIn("issue: No tree work — #5 is closed; write the task done",
                      [str(f) for f in report.issues])

    def test_an_unlanded_worktree_asks_the_user_instead(self):
        """Branch B: `wt-unmerged` carries a commit `main` never got — the same signal `reopen` already
        reads off a done task's tree — so the fault refuses to guess and hands the call to the user.
        No issue is ever reopened over this: the fix names a choice, not an action taken."""
        gh = FakeGh({"o/backlog": {5: "CLOSED", 6: "CLOSED", 7: "CLOSED"}})
        report = al.audit(self.workspace(), "2026-09-17", gh=gh)
        self.assertIn(
            "issue: Unlanded work — #6 is closed but its tree has work not on main; "
            "ask the user: reopen the issue or drop the work",
            [str(f) for f in report.issues])


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




# #45: "When a task's item names a merge request or pull request that has merged, the task still reads as
# stalled." The rule: a merged change on a task that isn't done is a `merged:` finding that says how to clear
# it: write the task done once the change is on main and green there, or write "merged" after the ref. The forge is
# injected (`gh` for GitHub, `call` for GitLab), so nothing here touches the network.
MERGE_SHA, OTHER_SHA = "c0ffee1" + "d" * 33, "b01dfac" + "e" * 33
FORBIDDEN_13 = {"type": "FORBIDDEN", "path": ["repository", "n13"], "message": "Resource not accessible"}
GL_URL, GH_URL = "https://gl.example/o/backlog/-/merge_requests/12", "https://github.com/o/backlog/pull/12"

CHANGE_TRACKER = ISSUE_TRACKER.split("| Bare |")[0] + "{rows}\n\n## Log\n\n- 09:00 opened the day\n"
STANDING_ROW = "| impl-3 — standing implementer | impl-3: standing implementer; wait idle | impl-3 | open | 09:00 |  |  |  | !12, PR #12 |"


def change_row(names: str, name: str = "Search", state: str = "running 09:10", issue: str = "#8",
               item: str = "Search pagination") -> str:
    """A task row whose checklist is `names`: what it says of its change (`!12`, `PR #12`, or the change's URL)."""
    return f"| {name} | {item} | robin | {state} | 09:00 |  | S | {issue} | {names} |"


def change_tracker(*rows: str) -> str:
    return CHANGE_TRACKER.format(rows="\n".join(rows))


def home(claude: str):
    return al.parse_coordinator(claude, today=datetime.now().date()).backlog


def printed(ref: str) -> str:
    """A change's ref as a row spells it: `!12` on GitLab, `PR #12` on GitHub, where a bare `#12` is an issue."""
    return f"PR {ref}" if ref.startswith("#") else ref


def change(ref: str, state: str = "merged", base: str = "release", sha: str = MERGE_SHA):
    """A change as `board_sources.change_states` answers it; only a merged one carries a merge sha."""
    return bs.ChangeState(ref, state, base, sha if state == "merged" else "")


class MergedChangeRuleTest(unittest.TestCase):
    """`merged_faults(tasks, changes, home, answered=True)`: one fault per live task (not standing, not done, its
    issue not in another repo) for the changes its row names and does not record as merged, unless one is open."""

    def faults(self, claude: str, *rows: str, changes: dict, **kw) -> list:
        return al.merged_faults(al.parse_tracker(change_tracker(*rows)).tasks, changes, home(claude), **kw)

    def test_an_unacknowledged_merged_change_on_a_live_task_is_one_fault(self):
        """The fault lists "each merged one as `<ref> merged into <base> <sha7>`", joined by `, `. On GitHub
        "the fault prints the ref the way a row spells it, `PR #277 merged into main 294cb30`"; the `changes`
        keys stay `#N` / `!N`."""
        gitlab, github = {"!12": change("!12")}, {"#12": change("#12")}
        both = {**gitlab, "!10": change("!10", sha=OTHER_SHA)}
        pulls = {**github, "#13": change("#13", sha=OTHER_SHA)}
        cases = (
            ("GitLab !N", GITLAB_CLAUDE, change_row("!12"), gitlab, ["!12"]),
            ("GitLab change URL", GITLAB_CLAUDE, change_row(GL_URL, state="open"), gitlab, ["!12"]),
            ("GitHub PR #N", ISSUE_CLAUDE, change_row("PR #12", state="waiting"), github, ["#12"]),
            ("GitHub change URL", ISSUE_CLAUDE, change_row(GH_URL, state="orphaned"), github, ["#12"]),
            ("every merged ref", GITLAB_CLAUDE, change_row("!10 then !12"), both, ["!10", "!12"]),
            ("one acknowledged, one not", GITLAB_CLAUDE, change_row("!10 merged, then !12"), both, ["!12"]),
            # The issue cell is part of the row scan.
            ("a change URL in the issue cell", GITLAB_CLAUDE, change_row("c", issue=GL_URL), gitlab, ["!12"]),
            # Acknowledged is "the ref, then only non-word characters, then the word `merged`". These are not.
            ("waiting for the merge", GITLAB_CLAUDE, change_row("waiting for !12 to be merged"), gitlab, ["!12"]),
            ("a bare mention", ISSUE_CLAUDE, change_row("Fix the regression PR #12 introduced"), github, ["#12"]),
            ("a longer word", GITLAB_CLAUDE, change_row("!12 mergedx"), gitlab, ["!12"]),
            # "only `PR #277 merged` or the URL form acknowledges"
            ("the bare number says merged", ISSUE_CLAUDE, change_row("#12 merged, held", item="Search pagination, PR #12"), github, ["#12"]),
            # "The second form still names #9001 (we do not parse prose after the ref)."
            ("another repo named after the ref", ISSUE_CLAUDE, change_row("upstream PR #12 in cli/cli"), github, ["#12"]),
            # "a loud wrong finding that the reader can clear beats a silent miss": whatever comes before `PR`,
            # the row names the home repo's #12, because "the audit cannot tell prose from a repo name".
            ("another repo named before the ref", ISSUE_CLAUDE, change_row("blocked on upstream cli/cli PR #12"), github, ["#12"]),
            ("a remote branch ends the sentence before", ISSUE_CLAUDE, change_row("Rebased onto origin/main. PR #12 is up"), github, ["#12"]),
            ("a branch named before the ref", ISSUE_CLAUDE, change_row("pushed feat/search PR #12"), github, ["#12"]),
            ("the home URL, then another ref", ISSUE_CLAUDE, change_row(f"{GH_URL} PR #13"), pulls, ["#12", "#13"]),
            # "a ref and a following `merged` in the NEXT cell never combine"
            ("merged starts the next cell", GITLAB_CLAUDE,
             change_row("c", name="Search for !12", item="merged docs follow"), gitlab, ["!12"]),
            ("merged starts the cell after an empty one", GITLAB_CLAUDE,
             change_row("merged docs follow", item="Search waits on !12", issue=""), gitlab, ["!12"]),
            # "Repo and host compared case-insensitively"; "No issue cell, or an unparseable one, is live."
            ("its issue names the home repo in another case", ISSUE_CLAUDE, change_row("PR #12", issue="O/Backlog#8"), github, ["#12"]),
            ("it has no issue", ISSUE_CLAUDE, change_row("PR #12", issue=""), github, ["#12"]),
            ("its issue cell is not a reference", ISSUE_CLAUDE, change_row("PR #12", issue="TBD"), github, ["#12"]),
        )
        for what, claude, row, changes, refs in cases:
            with self.subTest(what):
                faults = self.faults(claude, row, changes=changes)
                self.assertEqual(len(faults), 1, faults)
                line = str(faults[0])
                self.assertTrue(line.startswith("merged: Search"), line)
                said = line.split(" — ", 1)[1]
                for ref, c in changes.items():
                    if ref in refs:
                        # Seven characters of the sha, and no more, end what is said of each change.
                        self.assertRegex(said, rf"{re.escape(printed(ref))} merged into {c.base} {c.merge_sha[:7]}(?=[,;]|$)")
                    else:
                        self.assertNotIn(ref, said)
                # "a merged line tells the reader both ways to clear it": the task is done only once the change
                # is on main and green there, or the row records the merge by writing "merged" after the ref.
                _, cut, how = said.partition("; ")
                self.assertTrue(cut, line)
                for word in (r"\bdone\b", r"\bmain\b", '"merged"', r"\bafter\b"):
                    self.assertRegex(how, word)

    def test_the_refs_in_a_line_are_in_ascending_number_order(self):
        """"Refs in a line are in ascending number order", however the row orders them."""
        changes = {ref: change(ref) for ref in ("!9", "!10", "!12")}
        line = str(self.faults(GITLAB_CLAUDE, change_row("!12 after !9, with !10"), changes=changes)[0])
        self.assertEqual(re.findall(r"!\d+", line), ["!9", "!10", "!12"])

    def test_a_merge_with_no_sha_leaves_no_gap_in_the_line(self):
        """A merged ref reads `<ref> merged into <base> <sha7>`, with "no gap when the sha is empty"."""
        line = str(self.faults(GITLAB_CLAUDE, change_row("!12"), changes={"!12": change("!12", sha="")})[0])
        self.assertIn("!12 merged into release", line)
        for gap in ("  ", " ;"):
            self.assertNotIn(gap, line)

    def test_a_named_change_the_forge_does_not_have_is_not_found(self):
        """The fault lists, "only when `answered`, each missing one as `<ref> not found`", beside the merged
        ones; "a line with only not-found refs has no instruction clause". On GitHub "not-found reads
        `PR #999 not found`"."""
        merged = {"!12": change("!12")}
        self.assertEqual([str(f) for f in self.faults(GITLAB_CLAUDE, change_row("!12"), changes={})],
                         ["merged: Search — !12 not found"])
        self.assertEqual([str(f) for f in self.faults(ISSUE_CLAUDE, change_row("PR #999"), changes={})],
                         ["merged: Search — PR #999 not found"])
        for answered, found in ((True, True), (False, False)):
            with self.subTest("beside a merged one", answered=answered):
                faults = self.faults(GITLAB_CLAUDE, change_row("!12 then !99"), changes=merged, answered=answered)
                self.assertEqual(len(faults), 1, faults)
                for part in ("!12 merged into release", MERGE_SHA[:7]):
                    self.assertIn(part, str(faults[0]))
                self.assertEqual("!99 not found" in str(faults[0]), found, faults)
        # A forge that answered with an error may only have left the change out.
        self.assertEqual(self.faults(GITLAB_CLAUDE, change_row("!12"), changes={}, answered=False), [])

    def test_an_open_change_clears_only_the_task_that_names_it(self):
        """"Unless one is open" is per task: another task's merged change is still its own finding."""
        faults = self.faults(GITLAB_CLAUDE, change_row("!12"), change_row("!13", name="Export"),
                             changes={"!12": change("!12", "open"), "!13": change("!13")})
        self.assertEqual([f.task for f in faults], ["Export"])

    def test_no_fault(self):
        gitlab, github = {"!12": change("!12")}, {"#12": change("#12")}
        opened = {"!12": change("!12", "open")}
        cases = (
            ("its change is closed", GITLAB_CLAUDE, change_row("!12"), {"!12": change("!12", "closed")}),
            # "Any open unacknowledged named change: no fault."
            ("its change is open", GITLAB_CLAUDE, change_row("!12"), opened),
            ("a later change is still open", GITLAB_CLAUDE, change_row("!10 then !12"), {**opened, "!10": change("!10")}),
            ("one is open and one is missing", GITLAB_CLAUDE, change_row("!12 then !99"), opened),
            # "Live task: not standing, not done, issue not in another repo."
            ("the task is done", GITLAB_CLAUDE, change_row("!12", state="done 09:00–10:00"), gitlab),
            ("the row is a standing session's", GITLAB_CLAUDE, STANDING_ROW, gitlab),
            ("its issue is in another repo", ISSUE_CLAUDE, change_row("PR #12", issue="x/y#2"), github),
            ("its issue is in another project on the host", GITLAB_CLAUDE,
             change_row("!12", issue="https://gl.example/g/other/-/issues/2"), gitlab),
            # "Issue not in another repo, host compared": on a GitLab home the short form reads as GitHub's o/backlog.
            ("its issue is the home project's name on another host", GITLAB_CLAUDE, change_row("!12", issue="o/backlog#8"), gitlab),
            # "Acknowledged = the ref, then only non-word characters, then the word `merged` ... case-insensitive"
            ("!12 merged", GITLAB_CLAUDE, change_row("!12 merged, held for the release note"), gitlab),
            ("!12, merged", GITLAB_CLAUDE, change_row("!12, merged"), gitlab),
            ("**!12** merged", GITLAB_CLAUDE, change_row("**!12** merged"), gitlab),
            ("`!12` merged", GITLAB_CLAUDE, change_row("`!12` merged"), gitlab),
            ("!12 MERGED", GITLAB_CLAUDE, change_row("!12 MERGED"), gitlab),
            ("<url> merged", GITLAB_CLAUDE, change_row(f"{GL_URL} merged"), gitlab),
            ("PR #12 (merged)", ISSUE_CLAUDE, change_row("PR #12 (merged)"), github),
            ("PR #12: merged", ISSUE_CLAUDE, change_row("PR #12: Merged; deploy pending"), github),
            ("[PR #12](<pull url>) merged", ISSUE_CLAUDE, change_row(f"[PR #12]({GH_URL}) merged"), github),
            ("<pull url> merged", ISSUE_CLAUDE, change_row(f"{GH_URL} merged"), github),
            ("an acknowledged change the forge does not have", GITLAB_CLAUDE, change_row("!12 merged"), {}),
        )
        for what, claude, row, changes in cases:
            with self.subTest(what):
                self.assertEqual(self.faults(claude, row, changes=changes), [])


def gl_change(iid: int, state: str = "merged") -> dict:
    return {**tbs.gl_mr(iid), "state": state, "mergeCommitSha": MERGE_SHA if state == "merged" else None}


class ForgeGh(FakeGh):
    """FakeGh for `issue list`, and test_board_sources' FakePulls for `api graphql`."""

    def __init__(self, repos: dict, *pulls: dict, **answer):
        super().__init__(repos)
        self.pulls = tbs.FakePulls(*pulls, **answer)

    def __call__(self, args, input_text=None):
        return self.pulls(args) if args[:2] == ["api", "graphql"] else super().__call__(args)


class MergedChangeAuditTest(unittest.TestCase):
    """#45: "`audit` reports that fault next to the existing issue faults." The wording is MergedChangeRuleTest's."""

    def audit(self, claude: str, *rows: str, gh=None, call=None, **kw):
        tmp, self.root = issue_workspace(claude, change_tracker(*rows))
        self.addCleanup(tmp.cleanup)
        with patch.dict(os.environ, {"CHIEF_OF_STUFF_GITLAB_TOKEN": "tok"}), gitlab_issues(self):
            return al.audit(self.root, "2026-09-17", gh=gh, call=call, **kw)

    def assert_merged(self, report, tasks: list[str]) -> None:
        """The `merged:` lines printed are the report's findings, one per task named, counted in the summary."""
        self.assertEqual([f.task for f in report.merged], tasks, report.lines)
        self.assertEqual([line for line in report.lines if line.startswith("merged:")], [str(f) for f in report.merged])
        self.assertIn(f" merged={len(tasks)}", report.lines[-1])

    def test_an_in_progress_task_whose_merge_request_merged_is_flagged_with_the_sha(self):
        """#45's acceptance: "a mocked GitLab: an in-progress task names `!12`, and `!12` has merged. `audit`
        flags it with the merge sha." The adapter is "configured from the existing Backlog host, project and
        token", and asks once."""
        call = tbs.StrictGitLab(mrs=[gl_change(12)])
        report = self.audit(GITLAB_CLAUDE, change_row("!12"), call=call)
        self.assertEqual(call.posts, [("POST", "https://gl.example/api/graphql", "tok")])
        self.assertIn('"o/backlog"', call.queries[0])
        self.assert_merged(report, ["Search"])
        self.assertIn(MERGE_SHA[:7], str(report.merged[0]))

    def test_a_merge_request_the_forge_does_not_have_is_a_finding(self):
        """GitLab: "a missing iid is simply absent from nodes (no error)", so the row's `!12` is not found."""
        report = self.audit(GITLAB_CLAUDE, change_row("!12"), call=tbs.StrictGitLab())
        self.assert_merged(report, ["Search"])

    def test_a_merged_pull_request_and_a_closed_issue_are_both_findings(self):
        """GitHub's `PR #N`. "A task with a closed issue and a merged change simply gets both findings.\""""
        gh = ForgeGh(LIVE, tbs.gh_pull(12), tbs.gh_pull(13))
        report = self.audit(ISSUE_CLAUDE, change_row("PR #12"),
                            change_row("PR #13", name="Closed one", state="waiting", issue="#5"), gh=gh)
        self.assertEqual(len(gh.pulls.calls), 1)
        self.assertEqual([f.task for f in report.issues], ["Closed one"])
        self.assert_merged(report, ["Search", "Closed one"])

    def test_a_number_that_is_not_a_pull_request_is_not_found(self):
        """GitHub answers such a number with a NOT_FOUND error and a failing `gh`: that is `<ref> not found`
        on the task's line, beside any merged change, and never `merged: unknown`."""
        report = self.audit(ISSUE_CLAUDE, change_row("PR #999"), gh=ForgeGh(LIVE))
        self.assert_merged(report, ["Search"])
        self.assertEqual(str(report.merged[0]), "merged: Search — PR #999 not found")
        report = self.audit(ISSUE_CLAUDE, change_row("PR #12, PR #999"), gh=ForgeGh(LIVE, tbs.gh_pull(12)))
        self.assert_merged(report, ["Search"])
        for part in ("PR #12 merged into main", "PR #999 not found"):
            self.assertIn(part, str(report.merged[0]))

    def test_the_forge_is_asked_only_for_the_changes_live_rows_leave_unacknowledged(self):
        """`change_states` is called "once with exactly those numbers (not done rows', not other-repo rows')"."""
        gh = ForgeGh(LIVE, *(tbs.gh_pull(n) for n in (12, 13, 14, 15)))
        report = self.audit(ISSUE_CLAUDE, change_row("PR #12"),
                            change_row("PR #13", name="Finished", state="done 09:00–10:00", issue=""),
                            change_row("PR #14", name="Elsewhere", issue="x/y#2"),
                            change_row("PR #15 merged, held for the release", name="Held"), gh=gh)
        self.assertEqual((len(gh.pulls.calls), gh.pulls.asked), (1, {12}))
        self.assert_merged(report, ["Search"])

    def test_a_mistyped_issue_cell_elsewhere_is_not_a_forge_error(self):
        """The lean read "never asks for issues": an issue number the forge does not have is `issue_faults`' to
        report, and a `gh` that would answer NOT_FOUND for it is never asked."""
        gh = ForgeGh(LIVE, tbs.gh_pull(12))
        report = self.audit(ISSUE_CLAUDE, change_row("PR #12"), change_row("c", name="Typo", issue="#404"), gh=gh)
        self.assertEqual([f.task for f in report.issues], ["Typo"])
        self.assertEqual(gh.pulls.asked, {12})
        self.assert_merged(report, ["Search"])

    def test_no_live_row_naming_an_unacknowledged_change_makes_no_forge_call(self):
        """The forge is read only when "some live row names an unacknowledged change", and the summary line
        gains ` merged=N` "only when the read happened"."""
        rows = (("no row names a change", change_row("c")),
                ("only a done row names one", change_row("!12, PR #12", state="done 09:00–10:00", issue="")),
                ("only a standing row names one", STANDING_ROW),
                ("the only one named is recorded as merged", change_row("!12 merged, PR #12 merged; held for the release")))
        cases = [(what, claude, row) for claude in (ISSUE_CLAUDE, GITLAB_CLAUDE) for what, row in rows]
        cases.append(("only a row whose issue is in another repo names one", ISSUE_CLAUDE, change_row("PR #12", issue="x/y#2")))
        # "Issue not in another repo, host compared": on a GitLab home the short form reads as GitHub's o/backlog.
        cases.append(("only a row whose issue is on another host names one", GITLAB_CLAUDE, change_row("!12", issue="o/backlog#8")))
        # A change URL of another repo, project or host is not a change the row names.
        cases.append(("the only URL is another repo's pull request", ISSUE_CLAUDE, change_row("https://github.com/cli/cli/pull/12")))
        cases.append(("the only URL is another project's merge request", GITLAB_CLAUDE,
                      change_row("https://gl.example/g/other/-/merge_requests/12")))
        cases.append(("the only URL is another host's merge request", GITLAB_CLAUDE,
                      change_row("https://other.example/o/backlog/-/merge_requests/12")))
        for what, claude, row in cases:
            with self.subTest(what, claude=claude.splitlines()[-1]):
                gh, call = ForgeGh(LIVE, tbs.gh_pull(12)), tbs.StrictGitLab(mrs=[gl_change(12)])
                report = self.audit(claude, row, gh=gh, call=call)
                self.assertEqual((gh.pulls.calls, call.posts, report.merged), ([], [], []))
                self.assertNotIn("merged", report.lines[-1])

    def test_issues_off_makes_no_forge_call(self):
        """The forge is read only when `cfg.backlog and check_issues`: `--no-issues` is the offline audit."""
        for claude, names in ((ISSUE_CLAUDE, "PR #12"), (GITLAB_CLAUDE, "!12")):
            with self.subTest(claude=claude.splitlines()[-1]):
                gh, call = ForgeGh(LIVE, tbs.gh_pull(12)), tbs.StrictGitLab(mrs=[gl_change(12)])
                report = self.audit(claude, change_row(names), gh=gh, call=call, check_issues=False)
                self.assertEqual((gh.calls, gh.pulls.calls, call.posts, report.merged), ([], [], [], []))
                self.assertNotIn("merged", report.lines[-1])

    def test_a_forge_that_cannot_be_read_is_one_unknown_finding(self):
        """"Error → the known faults plus one `merged: unknown — <err>`; a raise → one `merged: unknown`."
        A change missing from an answer that came with an error is not `not found`."""
        forbidden = {"errors": [{"type": "FORBIDDEN", "message": "Resource not accessible"}]}
        cases = (("the forge answers an error", "PR #12", {"fail": (1, "", "gh: not logged in")}, "not logged in", []),
                 ("the forge raises", "PR #12", {"fail": RuntimeError("the wire fell out")}, "the wire fell out", []),
                 ("part of the answer and an error", "PR #12", forbidden, "Resource not accessible", ["Search"]),
                 ("a missing number and an error", "PR #999", forbidden, "Resource not accessible", []),
                 # "Any other error is an error", at a requested pull request's own path too: only NOT_FOUND there is not.
                 ("a pull request the token may not read", "PR #12, PR #13", {"errors": [FORBIDDEN_13]}, "Resource not accessible", ["Search"]),
                 # NOT_FOUND on the repository itself is not `PR #12 not found`.
                 ("the repository is not there", "PR #12", {"missing_repo": True}, "Could not resolve to a Repository", []))
        for what, names, answer, err, tasks in cases:
            with self.subTest(what):
                report = self.audit(ISSUE_CLAUDE, change_row(names), gh=ForgeGh(LIVE, tbs.gh_pull(12), **answer))
                unknown = [str(f) for f in report.merged if str(f).startswith("merged: unknown — ")]
                self.assertEqual(len(unknown), 1, report.lines)
                self.assertIn(err, unknown[0])
                self.assert_merged(report, tasks + ["unknown"])
                self.assertNotIn("not found", "\n".join(report.lines))

    def test_a_merge_request_missing_from_an_answer_with_an_error_is_not_not_found(self):
        """GitLab errors beside data: the states it gave still fault, and "a missing iid in such an answer is
        not `not found`"."""
        call = tbs.StrictGitLab(mrs=[gl_change(12)], errors=[{"message": "Internal server error"}])
        report = self.audit(GITLAB_CLAUDE, change_row("!12 then !99"), call=call)
        self.assert_merged(report, ["Search", "unknown"])
        self.assertIn("!12 merged into main", str(report.merged[0]))
        self.assertNotIn("not found", "\n".join(report.lines))

    def test_exit_code_counts_merged_findings(self):
        """`main()`'s exit code adds `len(report.merged)`: the open issue is no finding, the merged change is one."""
        tmp, root = issue_workspace(ISSUE_CLAUDE, change_tracker(change_row("PR #12")))
        self.addCleanup(tmp.cleanup)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(al.main(["--root", str(root), "--date", "2026-09-17"], gh=ForgeGh(LIVE, tbs.gh_pull(12))), 1)


class ShaFlagTest(unittest.TestCase):
    """#49: `audit --sha <sha>` fetches `origin main`, then says whether that commit is on `origin/main`.

    A peer says "the fix is on main at <sha>". The audit read only tracker rows' shas, against local `main`, and
    never fetched, so the claim was checked by hand with `git merge-base --is-ancestor <sha> origin/main`.
    One stdout line; the exit code is the verdict: 0 on, 1 not on, 2 unknown.
    """

    ROW = "| Open branch work | robin | done 10:00 | 09:00 |  | Checklist: Open branch work |"
    OWNERSHIP = "| robin | worktree wt-unmerged (feat/open) |"

    def setUp(self) -> None:
        tmp, self.root = workspace(self.ROW, self.OWNERSHIP)
        self.addCleanup(tmp.cleanup)
        self.clone = self.root / "repo"

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = al.main(["--root", str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def tip(self) -> str:
        return git("rev-parse", "--short", "origin/main", cwd=self.clone)

    def pushed_by_another_clone(self) -> str:
        other = self.root / "other"
        git("clone", "-q", str(self.root / "origin.git"), str(other), cwd=self.root)
        git("config", "user.email", "o@example.test", cwd=other)
        git("config", "user.name", "Other", cwd=other)
        (other / "fix.txt").write_text("fix\n")
        git("add", "fix.txt", cwd=other)
        git("commit", "-m", "fix", cwd=other)
        git("push", "-q", "origin", "main", cwd=other)
        return git("rev-parse", "HEAD", cwd=other)

    def test_a_commit_pushed_by_another_clone_is_found_by_fetching(self) -> None:
        """The issue's gap: the commit is on the remote's main and this clone has not fetched it."""
        new = self.pushed_by_another_clone()
        stale = self.tip()
        code, out, _ = self.run_main("--sha", new)
        self.assertNotEqual(self.tip(), stale)
        self.assertEqual(self.tip(), git("rev-parse", "--short", new, cwd=self.clone))
        self.assertEqual(out, f"sha {new}: on origin/main {self.tip()} (fetched)\n")
        self.assertEqual(code, 0)

    def test_an_abbreviated_sha_is_accepted(self) -> None:
        short = git("rev-parse", "--short", "origin/main", cwd=self.clone)
        code, out, _ = self.run_main("--sha", short)
        self.assertEqual((code, out), (0, f"sha {short}: on origin/main {self.tip()} (fetched)\n"))

    def test_a_commit_on_an_unmerged_branch_is_not_on_origin_main(self) -> None:
        sha = git("rev-parse", "feat/open", cwd=self.clone)
        code, out, _ = self.run_main("--sha", sha)
        self.assertEqual((code, out), (1, f"sha {sha}: not on origin/main {self.tip()} (fetched)\n"))

    def test_a_commit_on_local_main_not_pushed_is_not_on_origin_main(self) -> None:
        """Local `main` is what the row audit asks; `--sha` asks the remote's."""
        (self.clone / "local.txt").write_text("local\n")
        git("add", "local.txt", cwd=self.clone)
        git("commit", "-m", "local only", cwd=self.clone)
        sha = git("rev-parse", "main", cwd=self.clone)
        code, out, _ = self.run_main("--sha", sha)
        self.assertEqual((code, out), (1, f"sha {sha}: not on origin/main {self.tip()} (fetched)\n"))

    def test_a_commit_nobody_has_is_no_such_commit_after_fetch(self) -> None:
        sha = "0123456789abcdef0123456789abcdef01234567"
        code, out, _ = self.run_main("--sha", sha)
        self.assertEqual((code, out), (1, f"sha {sha}: no such commit after fetch, so not on origin/main {self.tip()}\n"))

    def test_fetch_failed_and_the_last_fetched_ref_holds_it(self) -> None:
        sha = git("rev-parse", "origin/main", cwd=self.clone)
        git("remote", "set-url", "origin", str(self.root / "gone.git"), cwd=self.clone)
        code, out, _ = self.run_main("--sha", sha)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("\n"), 1)
        self.assertTrue(out.startswith(f"sha {sha}: on origin/main {self.tip()} as last fetched \u2014 fetch failed: "), out)
        self.assertTrue(out.split("fetch failed: ", 1)[1].strip(), out)

    def test_fetch_failed_and_the_last_fetched_ref_does_not_hold_it(self) -> None:
        """No answer, not a no: the remote may hold it. Exit 2, for a commit nobody has and for one on a branch."""
        git("remote", "set-url", "origin", str(self.root / "gone.git"), cwd=self.clone)
        for what, sha in (("no such commit", "0123456789abcdef0123456789abcdef01234567"),
                          ("not an ancestor", git("rev-parse", "feat/open", cwd=self.clone))):
            with self.subTest(what):
                code, out, _ = self.run_main("--sha", sha)
                self.assertEqual(code, 2)
                self.assertEqual(out.count("\n"), 1)
                self.assertTrue(out.startswith(f"sha {sha}: unknown \u2014 fetch failed: "), out)
                self.assertTrue(out.endswith(f"; origin/main {self.tip()} as last fetched does not hold it\n"), out)

    def test_a_malformed_sha_is_refused_on_stderr(self) -> None:
        """Not 7\u201340 hex, as `SHA` reads a done state's citation. Nothing is fetched for it."""
        for bad in ("zzz", "abc123", "0123456789abcdef0123456789abcdef012345678", "--help-me", "main", "54b7eb3;ls"):
            with self.subTest(bad), patch.object(git_trees, "fetch_base", side_effect=AssertionError("fetched"), create=True):
                code, out, err = self.run_main(f"--sha={bad}")
                self.assertEqual((code, out), (2, ""))
                self.assertIn("audit_tasks:", err)

    def test_it_needs_no_tracker_for_the_day(self) -> None:
        (self.root / "daily" / "2026-09-17-tracker.md").unlink()
        sha = git("rev-parse", "origin/main", cwd=self.clone)
        for argv in (["--sha", sha], ["--sha", sha, "--date", "2026-09-17"]):
            with self.subTest(argv=argv[2:]):
                code, out, err = self.run_main(*argv)
                self.assertEqual((code, out, err), (0, f"sha {sha}: on origin/main {self.tip()} (fetched)\n", ""))

    def test_it_still_needs_the_config_root(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = al.main(["--root", d, "--sha", "0123456789abcdef"])
            self.assertEqual((code, out.getvalue()), (2, ""))
            self.assertIn("no CLAUDE.md", err.getvalue())

    def test_with_no_git_tree_there_is_nowhere_to_ask(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "CLAUDE.md").write_text(CLAUDE)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = al.main(["--root", d, "--sha", "0123456789abcdef"])
            self.assertEqual((code, out.getvalue()), (2, ""))
            self.assertIn("audit_tasks:", err.getvalue())

    def test_a_plain_audit_never_fetches_and_reads_as_before(self) -> None:
        """Guard: the fetch belongs to `--sha`. The row audit stays offline and its lines and exit do not move."""
        before = self.run_main("--date", "2026-09-17")
        real, fetched = subprocess.run, []

        def watch(argv, *a, **kw):
            if "fetch" in argv:
                fetched.append(argv)
            return real(argv, *a, **kw)

        with patch.object(git_trees, "fetch_base", side_effect=AssertionError("fetched"), create=True), \
                patch.object(git_trees, "check_sha", side_effect=AssertionError("checked"), create=True), \
                patch.object(subprocess, "run", watch):
            during = self.run_main("--date", "2026-09-17")
        self.assertEqual(fetched, [])
        self.assertEqual(during, before)
        code, out, _ = before
        self.assertEqual(code, 1)
        self.assertIn("reopen: wt-unmerged (feat/open): not on main \u2014 1 done task: Open branch work", out)
        self.assertNotIn("sha ", out)
