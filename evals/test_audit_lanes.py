"""Unit tests for scripts/audit_lanes.py: a lane is done when its change is on main, and a script says so, not a grep.

Repos here are real: a bare origin, a clone that has fetched it, and worktrees off that clone.
`git merge-base --is-ancestor` and `git status --porcelain` have no useful fake, and the point of the
script is that its answer is git's answer.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import audit_lanes as al  # noqa: E402

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

## Lanes

| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
{rows}

## Decisions

| time | item | Robin's words |
|---|---|---|

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


def workspace(rows: str, ownership: str) -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "daily").mkdir()
    (root / "CLAUDE.md").write_text(CLAUDE)
    (root / "daily" / "2026-09-17-tracker.md").write_text(TRACKER.format(rows=rows, ownership=ownership))
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

    def test_a_row_matches_by_lane_item_as_well_as_owner(self):
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
    def test_a_refusal_is_reported_once_however_many_lanes_name_it(self):
        rows = (
            "| One | robin | done 10:00 | 09:00 |  | Checklist: One |\n"
            "| Two | robin | done 10:01 | 09:00 |  | Checklist: Two |\n"
            "| Three | robin | done 10:02 | 09:00 |  | Checklist: Three |"
        )
        tmp, root = workspace(rows, "| robin | worktree wt-not-here |")
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(sum(1 for line in report.lines if "wt-not-here" in line), 1, report.lines)

    def test_a_long_lane_item_is_cut_short_in_the_reopen_line(self):
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
    def test_done_lane_reopens_when_its_tree_is_named_in_a_bullet(self):
        tmp, root = workspace(
            "| Open branch work | robin | done 10:00 | 09:00 |  | Checklist: Open branch work |",
            "- Open branch work: worktree `wt-unmerged` \u00b7 src/a/",
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual([r.lane for r in report.reopen], ["Open branch work"])


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
        self.assertEqual([r.lane for r in report.reopen], ["Open branch work"])
        self.assertIn("not on main", report.reopen[0].why)

    def test_done_with_uncommitted_work_must_reopen(self):
        tmp, root = workspace(
            "| Unsaved work | robin | done 10:00 | 09:00 |  | Checklist: Unsaved work |",
            "| robin | worktree wt-dirty (feat/dirty) |",
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual([r.lane for r in report.reopen], ["Unsaved work"])
        self.assertIn("uncommitted", report.reopen[0].why)

    def test_a_lane_with_no_tree_is_never_reopened(self):
        tmp, root = workspace(
            "| Decide the merge order | robin | done 10:00 | 09:00 |  | Checklist: Decide |",
            "| robin | Projects/notes.md |",
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(report.reopen, [])

    def test_a_running_lane_on_an_unmerged_branch_is_not_reopened(self):
        tmp, root = workspace(
            "| Still going | robin | running 09:30 | 09:00 |  | Checklist: Still going |",
            "| robin | worktree wt-unmerged (feat/open) |",
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(report.reopen, [])

    def test_two_done_lanes_one_landed_one_not(self):
        rows = (
            "| Landed work | robin | done 10:00 | 09:00 |  | Checklist: Landed work |\n"
            "| Open branch work | sam | done 10:30 | 09:00 |  | Checklist: Open branch work |"
        )
        ownership = "| robin | worktree wt-merged (landed) |\n| sam | worktree wt-unmerged (feat/open) |"
        tmp, root = workspace(rows, ownership)
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual([r.lane for r in report.reopen], ["Open branch work"])


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
        source = (Path(__file__).resolve().parent.parent / "scripts" / "audit_lanes.py").read_text()
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
