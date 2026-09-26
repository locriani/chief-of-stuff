"""flow_chart.py: the tracker's `stage:` Log lines become gantt rows, across days, with hold, merge and forecast."""

import re
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import board_sources as bs  # noqa: E402
import flow_chart as fc  # noqa: E402
import gantt  # noqa: E402
import one_shot  # noqa: E402
import render_board as rb  # noqa: E402
import settings as st  # noqa: E402
import tracker_write as tw  # noqa: E402

CT = ZoneInfo("America/Chicago")
NOW = datetime(2026, 9, 26, 2, 10, tzinfo=CT)
TODAY, YESTERDAY = NOW.date(), NOW.date() - timedelta(days=1)
H = timedelta(hours=1)
LANES = {"build": st.Lane(("implement", "pr", "review", "triage", "merge", "main"), ("triage", "merge"))}
KANBAN = st.Kanban(("todo", "doing", "decide"), "needs-review", {"implement": 0, "pr": 1, "review": 1, "triage": 2,
                   "merge": 2, "main": 1}, hold_stages=("triage",))

TODAY_TRACKER = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Upload size limit | Cap uploads | impl-1 | running 01:00 | 2026-09-25 |  | M | build | pr | #109 | c |
| Cache warmup | Warm the pool | Robin | waiting | 2026-09-26 |  | S | build | triage | #111 | c |
| Locale fallback | Fall back to en | Robin | done 00:41 | 2026-09-26 |  | S | build | main | #98 | c |
| Session timeout | Pick a timeout | unassigned | open | 2026-09-26 |  | S | build | implement | #107 | c |

## Log

- 00:05 opened the day
- 00:30 stage: Locale fallback → merge
- 00:41 stage: Locale fallback → main
- 01:00 stage: Upload size limit → pr
- 01:40 stage: Cache warmup → triage
"""
YESTERDAY_TRACKER = """# Tracker

## Resume

- 21:00 stage: Upload size limit → review

## Log

- 22:00 stage: Upload size limit → implement
- 23:00 stage: Cache warmup → review
"""


def moves() -> list[fc.Move]:
    return fc.moves(YESTERDAY_TRACKER, YESTERDAY, CT) + fc.moves(TODAY_TRACKER, TODAY, CT)


def rows(ends: dict | None = None, durations: dict | None = None, slots: int = 1,
         tracker: str = TODAY_TRACKER, approved: frozenset = frozenset()) -> dict[str, tuple[str, gantt.Row]]:
    tasks = rb.parse_tracker(tracker).tasks
    built = fc.build(moves(), tasks, LANES, {"Cache warmup"}, ends or {}, NOW, durations or {}, slots, approved)
    return {row.name: (status, row) for status, row in built}


def at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time(), tzinfo=CT)


class MovesTest(unittest.TestCase):
    def test_only_the_log_is_read(self):
        self.assertEqual(fc.moves(YESTERDAY_TRACKER, YESTERDAY, CT), [
            fc.Move(at(YESTERDAY, "22:00"), "Upload size limit", "implement"),
            fc.Move(at(YESTERDAY, "23:00"), "Cache warmup", "review")])

    def test_the_writer_line_is_the_line_read(self):
        body, name = tw.set_stage(TODAY_TRACKER, "session timeout", "pr", LANES)
        line = tw.append_log(body, f"- 02:05 stage: {name} → pr")
        self.assertEqual(fc.moves(line, TODAY, CT)[-1], fc.Move(at(TODAY, "02:05"), "Session timeout", "pr"))


class BuildTest(unittest.TestCase):
    def test_a_segment_runs_from_each_move_to_the_next_and_the_last_to_now(self):
        status, row = rows()["Upload size limit"]
        self.assertEqual((status, row.ref, row.note), ("running", "#109", "pr"))
        self.assertEqual([(g.category, g.start, g.end, g.kind) for g in row.segments], [
            ("implement", at(YESTERDAY, "22:00"), at(TODAY, "01:00"), "done"),
            ("pr", at(TODAY, "01:00"), NOW, "done")])

    def test_a_held_task_is_hatched_red_from_now_through_the_window(self):
        status, row = rows()["Cache warmup"]
        self.assertEqual((status, row.note), ("held", "hold · triage"))
        self.assertEqual([(g.category, g.kind) for g in row.segments], [("review", "done"), ("triage", "done"), ("triage", "hold")])
        hold = row.segments[-1]
        self.assertEqual(hold.start, NOW)
        self.assertGreaterEqual(hold.end, NOW + max(after for _, _, after in fc.WINDOWS))

    def test_the_last_stage_of_the_lane_ends_the_row(self):
        status, row = rows()["Locale fallback"]
        self.assertEqual((status, row.note), ("merged", "merged 00:41"))
        self.assertEqual([(g.category, g.start, g.end) for g in row.segments], [("merge", at(TODAY, "00:30"), at(TODAY, "00:41"))])

    def test_a_task_without_a_move_or_a_duration_has_no_row(self):
        self.assertNotIn("Session timeout", rows())

    def test_an_estimated_end_lays_the_remaining_stages_out_as_forecast(self):
        end = NOW + 4 * H
        _, row = rows({"Cap uploads": end})["Upload size limit"]
        ahead = [(g.category, g.start, g.end) for g in row.segments if g.kind == "forecast"]
        self.assertEqual(ahead, [("pr", NOW, NOW + H), ("review", NOW + H, NOW + 2 * H),
                                 ("triage", NOW + 2 * H, NOW + 3 * H), ("merge", NOW + 3 * H, end)])
        self.assertEqual(row.note, "pr · ~06:10")

    def test_an_approved_task_ends_with_its_forecast_merge(self):
        # Flow.dc.html's 24-hour chart: "approved · merge ~02:40"; with no estimate, its 7-day label "approved".
        got = rows({"Cap uploads": NOW + 4 * H}, approved=frozenset({"Upload size limit"}))
        status, row = got["Upload size limit"]
        self.assertEqual((status, row.note), ("running", "approved · merge ~06:10"))
        self.assertEqual(rows(approved=frozenset({"Upload size limit"}))["Upload size limit"][1].note, "approved")

    def test_a_held_task_gets_no_forecast(self):
        _, row = rows({"Warm the pool": NOW + 4 * H})["Cache warmup"]
        self.assertNotIn("forecast", [g.kind for g in row.segments])


QUEUED = TODAY_TRACKER.replace("| Session timeout |", "| Export format | Export CSV | unassigned | open | 2026-09-26 |  | M | build | implement | #112 | c |\n| Session timeout |")


class QueueTest(unittest.TestCase):
    """Open tasks at their lane's first stage with no move yet: forecast only, after the running forecasts, in tracker order."""

    def queued(self, slots: int = 1, ends: dict | None = None) -> dict[str, tuple[str, gantt.Row]]:
        return rows(ends if ends is not None else {"Cap uploads": NOW + 4 * H},
                    {"Export CSV": 5 * H, "Pick a timeout": 30 * H}, slots, QUEUED)

    def test_one_slot_runs_them_back_to_back_after_the_running_forecast(self):
        got = self.queued()
        export, timeout = got["Export format"][1], got["Session timeout"][1]
        self.assertEqual((export.segments[0].start, export.segments[-1].end), (NOW + 4 * H, NOW + 9 * H))
        self.assertEqual((timeout.segments[0].start, timeout.segments[-1].end), (NOW + 9 * H, NOW + 39 * H))
        self.assertEqual({g.kind for g in export.segments + timeout.segments}, {"forecast"})
        self.assertEqual([g.category for g in export.segments], ["implement", "pr", "review", "triage", "merge"])
        self.assertEqual((got["Export format"][0], export.ref, export.note), ("open", "#112", "queued · ~11:10"))
        self.assertEqual(timeout.note, "queued · Sun")
        self.assertEqual(list(got)[-2:], ["Export format", "Session timeout"])

    def test_free_slots_start_now(self):
        got = self.queued(slots=2)
        self.assertEqual(got["Export format"][1].segments[0].start, NOW)
        self.assertEqual(got["Session timeout"][1].segments[0].start, NOW + 4 * H)

    def test_more_running_than_slots_waits_for_a_slot(self):
        got = self.queued(ends={"Cap uploads": NOW + 4 * H, "Warm the pool": NOW + 2 * H})
        # The held task draws no forecast and so holds no slot: one running forecast, one slot.
        self.assertEqual(got["Export format"][1].segments[0].start, NOW + 4 * H)

    def test_nothing_is_laid_out_past_the_longest_window(self):
        got = rows({}, {"Export CSV": 200 * H, "Pick a timeout": H}, 1, QUEUED)
        self.assertIn("Export format", got)
        self.assertNotIn("Session timeout", got)

    def test_queued_rows_count_as_queued(self):
        html = fc.section(list(self.queued().values()), NOW)
        self.assertIn("1 merged · 1 running · 1 held · 2 queued · Thu 24 02:10 → Thu 1 02:10", html)


class SectionTest(unittest.TestCase):
    def test_no_move_no_section(self):
        self.assertEqual(fc.section([], NOW), "")

    def test_two_charts_with_counts_and_one_legend(self):
        html = fc.section(list(rows().values()), NOW)
        self.assertIn("<h2>Flow · 24 hours</h2>\n<div class=\"meta\">1 merged · 1 running · 1 held · 0 queued · Fri 20:10 → Sat 20:10</div>", html)
        self.assertIn("<h2>Flow · 7 days</h2>\n<div class=\"meta\">1 merged · 1 running · 1 held · 0 queued · Thu 24 02:10 → Thu 1 02:10</div>", html)
        self.assertEqual(html.count('<figure class="gantt"'), 2)
        self.assertEqual(html.count('<div class="gantt-legend">'), 1)
        self.assertLess(html.index("7 days"), html.index("gantt-legend"))

    def test_a_chart_draws_only_rows_in_its_window(self):
        old = fc.Move(NOW - 30 * H, "Old", "implement"), fc.Move(NOW - 29 * H, "Old", "main")
        built = fc.build(list(old), rb.parse_tracker(TODAY_TRACKER).tasks, {**LANES}, set(), {}, NOW)
        html = fc.section(built, NOW)
        self.assertNotIn("24 hours", html)
        self.assertIn("7 days", html)

    def test_the_legend_forecast_swatch_wears_implement(self):
        self.assertIn(".gantt-legend .gantt-forecast:not([data-cat]){--c:var(--stage-implement)}", fc.css())
        self.assertIn(".gantt-bar.gantt-forecast{background:repeating-linear-gradient(135deg,color-mix(in srgb,var(--c) 40%,transparent)",
                      fc.css())

    def test_bars_take_the_board_stage_tokens(self):
        css = fc.css()
        for stage in gantt.PALETTE:
            self.assertIn(f'.gantt-bar[data-cat="{stage}"]{{--c:var(--stage-{stage});', css)


class BoardTest(unittest.TestCase):
    CLAUDE = "# W\n\n## Coordinator\n\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n- Timezone: America/Chicago\n- Settings: `cos.toml`\n"

    def cfg(self):
        return rb.parse_coordinator(self.CLAUDE, today=TODAY)

    def test_flow_sits_after_build_and_the_panels(self):
        html = rb.render(TODAY_TRACKER, self.cfg(), NOW, lanes=LANES, tracker_day=TODAY, kanban=KANBAN,
                         stage_log=[(YESTERDAY, YESTERDAY_TRACKER)])
        build, panels, flow = html.index("<h2>Build</h2>"), html.index('class="panels"'), html.index("<h2>Flow · 24 hours</h2>")
        self.assertLess(build, panels)
        self.assertLess(panels, flow)
        self.assertIn('title="implement Fri 22:00–Sat 01:00', html)
        self.assertIn("gantt-hold", html)

    def test_queued_tasks_take_their_size_from_closed_tasks(self):
        tracker = TODAY_TRACKER.replace("| Locale fallback | Fall back to en | Robin | done 00:41 |", "| Locale fallback | Fall back to en | Robin | done 00:10–00:41 |")
        html = rb.render(tracker, self.cfg(), NOW, lanes=LANES, tracker_day=TODAY, kanban=KANBAN)
        self.assertIn("Session timeout", html[html.index("Flow · 24 hours"):])
        self.assertIn("queued · ~", html)

    def test_an_open_approved_change_labels_its_task_approved(self):
        change = bs.Change("!48", "u", "Cap uploads", "open", False, "passed", True, 1, None, "main", (), ("#109",))
        html = rb.render(TODAY_TRACKER, self.cfg(), NOW, lanes=LANES, tracker_day=TODAY, kanban=KANBAN,
                         sources=bs.Sources({}, {}, {"!48": change}, (), {}))
        self.assertIn(">approved · merge Sun<", html[html.index("Flow · 24 hours"):])
        html = rb.render(TODAY_TRACKER, self.cfg(), NOW, lanes=LANES, tracker_day=TODAY, kanban=KANBAN,
                         sources=bs.Sources({}, {}, {"!48": replace(change, approved=False)}, (), {}))
        self.assertNotIn(">approved", html[html.index("Flow · 24 hours"):])

    def test_no_stage_line_no_flow_and_no_chart_css(self):
        plain = TODAY_TRACKER.split("- 00:30")[0]
        html = rb.render(plain, self.cfg(), NOW, lanes=LANES, tracker_day=TODAY, kanban=KANBAN)
        self.assertNotIn("Flow ·", html)
        self.assertNotIn('class="gantt', html)
        self.assertNotIn(".gantt-bar", html)

    def test_every_stage_colour_is_set_light_and_dark(self):
        html = rb.render(TODAY_TRACKER, self.cfg(), NOW, lanes=LANES, tracker_day=TODAY, kanban=KANBAN)
        used = set(re.findall(r"--c:var\((--stage-[\w-]+)\)", html))
        self.assertEqual(used, {f"--stage-{s}" for s in gantt.PALETTE})
        blocks = [re.search(r"^:root\{(.*?)\}$", html, re.M)[1],
                  re.search(r'^@media \(prefers-color-scheme:dark\)\{:root:not\(\[data-theme="light"\]\)\{(.*?)\}', html, re.M)[1],
                  re.search(r'^:root\[data-theme="dark"\]\{(.*?)\}', html, re.M)[1]]
        for block in blocks:
            self.assertEqual(used - set(re.findall(r"(--stage-[\w-]+):#", block)), set())

    def test_main_reads_the_trackers_the_window_covers(self):
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(self.CLAUDE)
        (root / "cos.toml").write_text('[lanes]\nbuild = { stages = ["implement", "pr", "review", "triage", "merge", "main"] }\n')
        (root / "daily").mkdir()
        today = datetime.now(CT).date()
        (root / f"daily/{today}-tracker.md").write_text(TODAY_TRACKER)
        (root / f"daily/{today - timedelta(days=1)}-tracker.md").write_text(YESTERDAY_TRACKER)
        (root / f"daily/{today - timedelta(days=5)}-tracker.md").write_text(YESTERDAY_TRACKER.replace("Upload size limit", "Too old"))
        html = rb.main(["--root", str(root)]).read_text()
        self.assertIn('title="implement ', html)
        self.assertNotIn("Too old", html)


ONE_SHOT_TRACKER = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Audit headers | Security audit | unassigned | open | 2026-09-26 |  | S |  |  | #120 | c |
| Trim logs | Log trim | unassigned | open | 2026-09-26 |  | S |  |  | #121 | c |
| Pin fonts | Font pin | unassigned | open | 2026-09-26 |  | S |  |  | #122 | c |

## File ownership

| context | paths |
|---|---|
| Security audit | `src/a/` |
| Log trim | `src/b/` |
| Font pin | `src/c/` |

## Log

- 00:05 opened the day
"""


REVIEW_LOOP = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Documentation upload | Upload docs | w1 | waiting | 2026-09-26 |  | S | build | pr | #307 | c |
| Review !182 | Review 182 | w2 | done 00:50–01:20 | 2026-09-26 |  | S | build | review | #307 | c |
| Fix !182 R1 | Fix 182 | w3 | running 01:20 | 2026-09-26 |  | S | build | triage | #307 | c |
| Tidy config | Tidy | w4 | running 00:40 | 2026-09-26 |  | S |  |  |  | c |
| Trim cache | Trim | w5 | running 00:45 | 2026-09-26 |  | S |  |  |  | c |

## Log

- 00:10 stage: Documentation upload → implement
- 00:40 stage: Documentation upload → pr
- 00:40 stage: Tidy config → implement
- 00:45 stage: Trim cache → implement
- 00:50 stage: Review !182 → review
- 01:20 stage: Fix !182 R1 → triage
"""


class IssueRowTest(unittest.TestCase):
    """The review loop makes a Tasks row per pass; the chart draws one row per issue (#155)."""

    def built(self) -> list[tuple[str, gantt.Row]]:
        return fc.build(fc.moves(REVIEW_LOOP, TODAY, CT), rb.parse_tracker(REVIEW_LOOP).tasks, LANES, set(), {}, NOW)

    def test_tasks_sharing_an_issue_draw_on_one_row_in_time_order(self):
        mine = [(s, r) for s, r in self.built() if r.ref == "#307"]
        self.assertEqual(len(mine), 1)
        status, row = mine[0]
        # Named for the first task, with the status of the latest.
        self.assertEqual((status, row.name), ("running", "Documentation upload"))
        starts = [g.start for g in row.segments]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual([g.category for g in row.segments][:4], ["implement", "pr", "review", "triage"])

    def test_a_task_with_no_segment_still_merges(self):
        # A task whose only move is its lane's last stage draws no segment; its issue row still renders.
        text = REVIEW_LOOP.replace("| Trim cache | Trim | w5 | running 00:45 | 2026-09-26 |  | S |  |  |  | c |",
                                   "| Land docs | Land | w6 | done 01:00 | 2026-09-26 |  | S | build | main | #307 | c |")
        text = tw.append_log(text, "- 01:00 stage: Land docs → main")
        got = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, set(), {}, NOW)
        self.assertEqual(len([r for _, r in got if r.ref == "#307"]), 1)
        fc.section(got, NOW)

    def test_tasks_without_an_issue_keep_a_row_each(self):
        names = [r.name for _, r in self.built() if not r.ref]
        self.assertEqual(sorted(names), ["Tidy config", "Trim cache"])


class OneShotTest(unittest.TestCase):
    """A task with no `stage:` lines draws from the one-shot launcher's own Log lines, at the times they record (#149)."""

    def tracker(self) -> str:
        # The launcher's real writers produce the lines the chart reads.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tracker.md"
            path.write_text(ONE_SHOT_TRACKER)
            one_shot.record_launch(path, "Security audit", "w1", "worktree `a` (a)", "codex", "", "00:20")
            one_shot.record_launch(path, "Log trim", "w2", "worktree `b` (b)", "codex", "", "01:00")
            one_shot.record_launch(path, "Font pin", "w3", "worktree `c` (c)", "codex", "", "00:30")
            one_shot.update_tracker(path, "Security audit", "w1", "Robin", "done", "fixed", "a.py", "01:30")
            one_shot.update_tracker(path, "Font pin", "w3", "Robin", "human_review", "unclear", "c.py", "01:10")
            return path.read_text()

    def built(self, text: str) -> dict[str, tuple[str, gantt.Row]]:
        got = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, set(), {}, NOW)
        return {row.name: (status, row) for status, row in got}

    def test_a_running_one_shot_draws_from_its_launch_to_now(self):
        status, row = self.built(self.tracker())["Trim logs"]
        self.assertEqual((status, row.ref), ("running", "#121"))
        self.assertEqual([(g.category, g.start, g.end) for g in row.segments],
                         [("implement", at(TODAY, "01:00"), NOW)])

    def test_a_finished_one_shot_moves_on_at_its_completion(self):
        status, row = self.built(self.tracker())["Audit headers"]
        self.assertEqual(status, "waiting")
        self.assertEqual([(g.category, g.start, g.end) for g in row.segments],
                         [("implement", at(TODAY, "00:20"), at(TODAY, "01:30")), ("pr", at(TODAY, "01:30"), NOW)])

    def test_a_one_shot_needing_review_moves_to_review(self):
        _, row = self.built(self.tracker())["Pin fonts"]
        self.assertEqual([(g.category, g.start, g.end) for g in row.segments],
                         [("implement", at(TODAY, "00:30"), at(TODAY, "01:10")), ("review", at(TODAY, "01:10"), NOW)])

    def test_a_done_task_ends_at_its_done_time(self):
        text = self.tracker().replace("| Robin | waiting |", "| Robin | done 00:20–01:50 |", 1)
        _, row = self.built(text)["Audit headers"]
        self.assertEqual(row.segments[-1].end, at(TODAY, "01:50"))
        self.assertTrue(all(g.end <= at(TODAY, "01:50") for g in row.segments))

    def test_stage_lines_take_over_from_their_first_move(self):
        # A coordinator that starts writing stage moves mid-run keeps the history the launcher recorded before them.
        text = tw.append_log(self.tracker(), "- 01:40 stage: Audit headers → review")
        text = tw.append_log(text, "- 01:50 one-shot w9 started: codex, worktree `a`, task Security audit")
        _, row = self.built(text)["Audit headers"]
        self.assertEqual([(g.category, g.start, g.end) for g in row.segments],
                         [("implement", at(TODAY, "00:20"), at(TODAY, "01:30")), ("pr", at(TODAY, "01:30"), at(TODAY, "01:40")),
                          ("review", at(TODAY, "01:40"), NOW)])


if __name__ == "__main__":
    unittest.main()
