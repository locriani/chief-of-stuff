"""tracker_log.py: each Log line the scripts write reads back with the pattern its readers use, worded as it always was."""

import sys
import unittest
from datetime import date, time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import tracker_log as tl  # noqa: E402

CT = ZoneInfo("America/Chicago")
DAY = date(2026, 9, 18)
TREE = "worktree `trees/up` (fix-upload)"  # as one_shot._tree_note names a tree in File ownership


class RoundTripTest(unittest.TestCase):
    """Format a line, then read its fields back."""

    def test_a_stage_line(self):
        line = tl.stage_line("11:05", "Upload path check", "pr")
        self.assertEqual(tl.STAGE.match(line).groups(), ("11", "05", "Upload path check", "pr"))

    def test_a_started_line(self):
        line = tl.started_line("09:15", "worker01", "codex", "gpt-5", TREE, "Security audit")
        self.assertEqual(tl.STARTED.match(line).groups(), ("09", "15", "worker01", "Security audit"))
        self.assertEqual(tl.STARTED_DETAIL.match(line).groups(),
                         ("09:15", "worker01", "codex gpt-5", "trees/up", "Security audit"))
        bare = tl.started_line("09:15", "worker01", "claude", "", TREE, "Security audit")
        self.assertEqual(tl.STARTED_DETAIL.match(bare)[3], "claude")

    def test_a_started_line_with_an_effort_after_the_model_is_still_read(self):
        # (#52) What the pages read: STARTED (workers page, moves) and STARTED_DETAIL (issue page) both match it,
        # and the time, name, tree and task come out as without an effort.
        line = "- 09:15 one-shot worker01 started: codex gpt-test effort=medium, worktree `trees/up`, task Security audit"
        self.assertEqual(tl.STARTED.match(line).groups(), ("09", "15", "worker01", "Security audit"))
        got = tl.STARTED_DETAIL.match(line)
        self.assertEqual((got[1], got[2], got[4], got[5]), ("09:15", "worker01", "trees/up", "Security audit"))
        moves = tl.moves(f"## Log\n\n{line}\n", DAY, CT)
        self.assertEqual([(m.at.strftime("%H:%M"), m.name, m.stage, m.launch, m.worker) for m in moves],
                         [("09:15", "Security audit", "implement", True, "worker01")])

    def test_an_ended_line(self):
        for status, outcome in (("done", tl.COMPLETED), ("human_review", tl.HUMAN_REVIEW)):
            with self.subTest(status=status):
                line = tl.ended_line("10:20", "worker01", status, "fixed the cap", "a.py b.py")
                self.assertEqual(tl.ENDED.match(line).groups(), ("10", "20", "worker01", outcome))
                self.assertIsNone(tl.RELAUNCH.match(line))

    def test_a_relaunch_line(self):
        line = tl.relaunch_line("10:20", "worker01", "Security audit", "target PR merged before work began")
        self.assertEqual(tl.RELAUNCH.match(line).groups(), ("10", "20", "worker01", "relaunch"))
        self.assertIsNone(tl.ENDED.match(line))
        # The launcher finds an earlier relaunch of the task by this text.
        self.assertIn(tl.RELAUNCHED.format(task="Security audit"), line)

    def test_a_relaunch_line_does_not_double_a_reasons_final_period(self):
        # #436: a quota stop's reset text ends in a period of its own.
        marker = tl.RELAUNCHED.format(task="Security audit")
        for reason, tail in (("Resets in 3h46m46s.", "Resets in 3h46m46s."), ("base moved", "base moved."), ("", ".")):
            with self.subTest(reason=reason):
                self.assertEqual(tl.relaunch_line("10:20", "worker01", "Security audit", reason),
                                 f"- 10:20 one-shot worker01{marker}{tail}")

    def test_a_relaunch_line_keeps_a_reason_that_ends_in_several_periods_unchanged(self):
        marker = tl.RELAUNCHED.format(task="Security audit")
        for reason in ("waiting..", "waiting..."):
            with self.subTest(reason=reason):
                self.assertEqual(tl.relaunch_line("10:20", "worker01", "Security audit", reason),
                                 f"- 10:20 one-shot worker01{marker}{reason}")

    def test_an_ended_line_does_not_double_a_reasons_final_period(self):
        for reason, tail in (("Resets in 3h46m46s.", "Resets in 3h46m46s."), ("fixed the cap", "fixed the cap."), ("", ".")):
            with self.subTest(reason=reason):
                line = tl.ended_line("10:20", "worker01", "human_review", reason, "a.py")
                self.assertEqual(line, f"- 10:20 one-shot worker01: {tl.HUMAN_REVIEW} — {tail} Changes: a.py.")

    def test_the_moves_read_from_the_written_lines(self):
        text = "\n".join(("# Tracker", "", "## Log", "",
                          tl.started_line("09:15", "worker01", "codex", "", TREE, "Security audit"),
                          tl.stage_line("09:40", "Upload path check", "review"),
                          tl.ended_line("10:20", "worker01", "done", "fixed", "a.py"),
                          tl.started_line("10:30", "worker02", "claude", "opus", TREE, "Cap uploads"),
                          tl.ended_line("11:00", "worker02", "human_review", "unclear", "b.py"),
                          tl.relaunch_line("11:10", "worker01", "Security audit", "base moved"), ""))
        self.assertEqual([(m.at.time(), m.name, m.stage, m.launch, m.worker) for m in tl.moves(text, DAY, CT)], [
            (time(9, 15), "Security audit", "implement", True, "worker01"),
            (time(9, 40), "Upload path check", "review", False, ""),
            (time(10, 20), "Security audit", "pr", True, "worker01"),
            (time(10, 30), "Cap uploads", "implement", True, "worker02"),
            (time(11, 0), "Cap uploads", "review", True, "worker02")])


class WordingTest(unittest.TestCase):
    """One line of each kind, byte for byte as `chief-of-stuff log --stage` and the one-shot launcher wrote it
    before they shared these formatters. Old trackers hold these lines, and the pages still read them."""

    def test_each_kind_is_worded_as_before(self):
        for line, before in (
            (tl.stage_line("11:05", "Upload path check", "pr"), "- 11:05 stage: Upload path check → pr"),
            (tl.started_line("09:15", "worker01", "codex", "gpt-5", TREE, "Security audit"),
             "- 09:15 one-shot worker01 started: codex gpt-5, worktree `trees/up`, task Security audit"),
            (tl.started_line("09:15", "worker01", "claude", "", "worktree `w` (w)", "Security audit"),
             "- 09:15 one-shot worker01 started: claude, worktree `w`, task Security audit"),
            (tl.ended_line("10:20", "worker01", "done", "fixed the cap", "a.py b.py"),
             "- 10:20 one-shot worker01: completed; awaiting integration — fixed the cap. Changes: a.py b.py."),
            (tl.ended_line("10:20", "worker01", "human_review", "needs a decision", "changed parser"),
             "- 10:20 one-shot worker01: HUMAN REVIEW NEEDED — needs a decision. Changes: changed parser."),
            (tl.relaunch_line("10:20", "worker01", "Security audit", "target PR merged before work began"),
             "- 10:20 one-shot worker01: relaunch requested for Security audit — target PR merged before work began."),
        ):
            with self.subTest(before=before):
                self.assertEqual(line, before)


class RelaunchStopTest(unittest.TestCase):
    """#457: the written relaunch request records the matched worker's stop time."""

    def test_a_relaunch_requested_stop_is_read_at_its_stop_time(self):
        started = tl.started_line("09:15", "worker01", "codex", "gpt-test", TREE, "Check parser")
        stopped = tl.relaunch_line("10:20", "worker01", "Check parser", "base moved")
        text = f"## Log\n\n{started}\n{stopped}\n"
        # Assert the boundary and its identity without prescribing an internal stage name for a stop.
        self.assertEqual([(m.at.date(), m.at.time(), m.name, m.launch, m.worker, m.line)
                          for m in tl.moves(text, DAY, CT)], [
            (DAY, time(9, 15), "Check parser", True, "worker01", started),
            (DAY, time(10, 20), "Check parser", True, "worker01", stopped)])

    def test_an_unmatched_relaunch_does_not_stop_another_worker(self):
        started = tl.started_line("09:15", "worker01", "codex", "gpt-test", TREE, "Check parser")
        stopped = tl.relaunch_line("10:20", "worker02", "Check parser", "base moved")
        text = f"## Log\n\n{started}\n{stopped}\n"
        self.assertEqual([(m.at.time(), m.name, m.stage, m.worker) for m in tl.moves(text, DAY, CT)],
                         [(time(9, 15), "Check parser", "implement", "worker01")])


if __name__ == "__main__":
    unittest.main()
