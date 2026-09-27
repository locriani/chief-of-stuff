"""flow_chart.py: the tracker's `stage:` Log lines become gantt rows, across days, with hold, merge and forecast."""

import re
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
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
    log = fc.moves(YESTERDAY_TRACKER, YESTERDAY, CT) + fc.moves(tracker, TODAY, CT)
    built = fc.build(log, tasks, LANES, {"Cache warmup"}, ends or {}, NOW, durations or {}, slots, approved)
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


DONE_NO_CLOCK = TODAY_TRACKER.replace("| Upload size limit | Cap uploads | impl-1 | running 01:00 |",
                                      "| Upload size limit | Cap uploads | impl-1 | done |")
DONE_HHMM = TODAY_TRACKER.replace("| Upload size limit | Cap uploads | impl-1 | running 01:00 |",
                                  "| Upload size limit | Cap uploads | impl-1 | done 01:30 |")
DONE_RANGE = TODAY_TRACKER.replace("| Upload size limit | Cap uploads | impl-1 | running 01:00 |",
                                   "| Upload size limit | Cap uploads | impl-1 | done 00:50–01:30 |")

DONE_MERGE_TRACKER = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Part one | Part one work | w1 | done | 2026-09-26 |  | S | build | pr | #400 | c |
| Part two | Part two work | w2 | done | 2026-09-26 |  | S | build | review | #400 | c |

## Log

- 00:10 stage: Part one → implement
- 00:40 stage: Part one → pr
- 00:50 stage: Part two → review
"""

STILL_RUNS_TO_NOW = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Open task | Open work | w1 | open | 2026-09-26 |  | S | build | implement | #401 | c |
| Running task | Running work | w2 | running 00:20 | 2026-09-26 |  | S | build | pr | #402 | c |
| Waiting task | Waiting work | w3 | waiting | 2026-09-26 |  | S | build | review | #403 | c |

## Log

- 00:10 stage: Open task → implement
- 00:20 stage: Running task → pr
- 00:30 stage: Waiting task → review
"""


class DoneNoClockTest(unittest.TestCase):
    """A `done` task's last bar ends at its last move, not now (#196)."""

    def test_a_done_task_with_no_clock_draws_no_bar_past_its_last_move(self):
        last_move = at(TODAY, "01:00")  # Upload size limit's last `stage:` move in TODAY_TRACKER's Log
        _, row = rows(tracker=DONE_NO_CLOCK)["Upload size limit"]
        self.assertTrue(row.segments)
        for g in row.segments:
            with self.subTest(segment=g):
                self.assertLessEqual(g.end, last_move)
        self.assertEqual(row.segments[-1].end, last_move)

    def test_a_done_time_or_range_still_ends_the_last_bar_there(self):
        for label, tracker in (("done HH:MM", DONE_HHMM), ("done HH:MM–HH:MM", DONE_RANGE)):
            with self.subTest(state=label):
                _, row = rows(tracker=tracker)["Upload size limit"]
                self.assertEqual(row.segments[-1].end, at(TODAY, "01:30"))

    def test_several_done_no_clock_tasks_sharing_an_issue_merge_ending_at_the_latest_move(self):
        tasks = rb.parse_tracker(DONE_MERGE_TRACKER).tasks
        built = fc.build(fc.moves(DONE_MERGE_TRACKER, TODAY, CT), tasks, LANES, set(), {}, NOW)
        mine = [(s, r) for s, r in built if r.ref == "#400"]
        self.assertEqual(len(mine), 1)
        _, row = mine[0]
        latest_move = at(TODAY, "00:50")  # Part two's `stage:` move, the last move in the whole group
        for g in row.segments:
            with self.subTest(segment=g):
                self.assertLessEqual(g.end, latest_move)
        # (#196) line 1: no bar past ITS OWN task's last move, even once the group merges the two tasks'
        # segments into one row. A bar's title is its owner (no launcher lines in this fixture), so map
        # owner back to the task that owns it and its own last `stage:` move, both read from the fixture,
        # not hardcoded.
        own_last_move = {}
        for m in fc.moves(DONE_MERGE_TRACKER, TODAY, CT):
            own_last_move[m.name] = max(own_last_move.get(m.name, m.at), m.at)
        owner_task = {t.owner.strip(): t.name.strip() for t in tasks}
        for g in row.segments:
            with self.subTest(segment=g):
                self.assertLessEqual(g.end, own_last_move[owner_task[g.title]])

    def test_an_open_running_or_waiting_task_still_runs_its_last_bar_to_now(self):
        built = {row.name: (status, row) for status, row in
                 fc.build(fc.moves(STILL_RUNS_TO_NOW, TODAY, CT), rb.parse_tracker(STILL_RUNS_TO_NOW).tasks,
                          LANES, set(), {}, NOW)}
        for name in ("Open task", "Running task", "Waiting task"):
            with self.subTest(task=name):
                _, row = built[name]
                self.assertTrue(row.segments)
                self.assertEqual(row.segments[-1].end, NOW)


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

# #182: a launch records its task as the item read then; the row's item changes afterwards.
UPLOAD_ITEM = "Cap uploads at 25 MB"
UPLOAD_PROMPT = (f"{UPLOAD_ITEM}. The form refuses a larger file before sending it and names the limit. Keep the limit "
                 "in one setting that both sides read. Add tests for exactly 25 MB and one byte over.")
KEY_PROMPT = ("Rotate the signing key for the token service (#105). Swap the key in the vault before the restart, "
              "and note the new key id in the runbook.")
SWEEP_PROMPT = ("Sweep the stale branches older than thirty days in every repository and list each one that still "
                "has an open change against it.")
PROMPT_SENTENCES = ("Keep the limit in one setting that both sides read", "Swap the key in the vault before the restart")
# (row name, launch-time item, worker, tree, launched at, the item afterwards; None = the row is gone)
LAUNCHES = (("Upload size limit", UPLOAD_PROMPT, "impl-1", "upload", "00:20", UPLOAD_ITEM),
            ("Key rotation", KEY_PROMPT, "impl-2", "keys", "00:40", "Monthly key rotation"),
            ("Branch sweep", SWEEP_PROMPT, "impl-3", "sweep", "01:00", None))
LAUNCHED_TRACKER = f"""# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Upload size limit | {UPLOAD_PROMPT} | unassigned | open | {TODAY} |  | M |  |  | #104 | c |
| Key rotation | {KEY_PROMPT} | unassigned | open | {TODAY} |  | S |  |  | #105 | c |
| Branch sweep | {SWEEP_PROMPT} | unassigned | open | {TODAY} |  | S |  |  | #106 | c |

## File ownership

| context | paths |
|---|---|
| Upload size limit | `src/upload/` |
| Key rotation | `src/keys/` |
| Branch sweep | `tools/sweep/` |

## Log

- 00:05 opened the day
"""


def launched_tracker() -> str:
    """The launcher's real writer records each launch; then Upload's item shrinks to the prompt's first words, Key
    rotation's item is reworded (its prompt still carries #105), and Branch sweep's row is removed."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tracker.md"
        path.write_text(LAUNCHED_TRACKER)
        for _, prompt, worker, tree, hhmm, _ in LAUNCHES:
            one_shot.record_launch(path, prompt, worker, f"worktree `trees/{tree}` ({tree})", "claude", "opus", hhmm)
        text = path.read_text()
    for name, prompt, *_, after in LAUNCHES:
        row = f"| {name} | {prompt} |"
        text = (text.replace(row, f"| {name} | {after} |") if after else
                "".join(line for line in text.splitlines(keepends=True) if not line.startswith(row)))
    return text


def many_launched_tracker(n: int) -> tuple[str, dict[str, tuple[str, str, str]]]:
    """n rows, each launched once by the launcher's real writer, and {row name: (its ref, its launch HH:MM, the
    launch's task text)}. Even
    rows' items shrink afterwards to the launch text's first words; odd rows' items are reworded, so only the issue
    ref their launch text carries maps them (#184). Each launch is recorded on its own one-row tracker, then the
    rows and lines are joined, so the fixture costs O(n)."""
    head = LAUNCHED_TRACKER.split("| Upload size limit |", 1)[0]
    parts, expected = {"## Tasks": [], "## File ownership": [], "## Log": []}, {}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tracker.md"
        for i in range(n):
            name, ref, hhmm = f"Chore {i:04d}", f"#{10000 + i}", f"{i % 120 // 60:02d}:{i % 60:02d}"
            item, after = ((f"Tidy module {i:04d}. Keep its public names and add a test for each change.", f"Tidy module {i:04d}")
                           if i % 2 == 0 else
                           (f"Rename setting {i:04d} ({ref}). Update every reader in the same change.", f"Setting {i:04d} rename"))
            path.write_text(f"{head}| {name} | {item} | unassigned | open | {TODAY} |  | S |  |  | {ref} | c |\n\n"
                            f"## File ownership\n\n| context | paths |\n|---|---|\n| {name} | `src/m{i:04d}/` |\n\n## Log\n")
            one_shot.record_launch(path, item, f"w{i}", f"worktree `trees/t{i}` (t{i})", "claude", "opus", hhmm)
            text = path.read_text().replace(f"| {name} | {item} |", f"| {name} | {after} |")
            for heading, lines in parts.items():
                lines += [line for line in rb._section(text, heading) if line.startswith(("| C", "- "))]
            expected[name] = (ref, hhmm, item)
    return (head + "\n".join(parts["## Tasks"]) + "\n\n## File ownership\n\n| context | paths |\n|---|---|\n"
            + "\n".join(parts["## File ownership"]) + "\n\n## Log\n\n" + "\n".join(parts["## Log"]) + "\n"), expected


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


class LaunchRowTest(unittest.TestCase):
    """A one-shot's launch is drawn on its task's row even after the row's item stops matching the launch (#182)."""

    def setUp(self):
        text = launched_tracker()
        self.got = [row for _, row in fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, set(), {}, NOW)]

    def launched_at(self, hhmm: str) -> gantt.Row:
        rows = [r for r in self.got if any(g.start == at(TODAY, hhmm) for g in r.segments)]
        self.assertEqual(len(rows), 1, [r.name[:40] for r in self.got])
        return rows[0]

    def test_a_launch_is_drawn_on_its_tasks_own_row(self):
        # Rules 1–3: a started line whose task begins with a row's item, or carries the row's issue ref, is drawn on
        # that row, with its ref and name; no launcher-only row is named by the prompt.
        for hhmm, ref, name in (("00:20", "#104", "Upload size limit"), ("00:40", "#105", "Key rotation")):
            with self.subTest(task=name):
                row = self.launched_at(hhmm)
                self.assertEqual((row.ref, row.name), (ref, name))
        html = fc.section([("running", r) for r in self.got], NOW)
        for sentence in PROMPT_SENTENCES:
            with self.subTest(sentence=sentence):
                self.assertFalse(sentence in html, "the prompt's sentence is drawn")

    def test_no_row_name_is_longer_than_its_tasks_name(self):
        # Rule 4: no Flow row name is longer than the row name it maps to.
        for name, _, _, _, hhmm, after in LAUNCHES:
            if after:
                with self.subTest(task=name):
                    self.assertLessEqual(len(self.launched_at(hhmm).name), len(name))

    def test_a_launch_with_no_row_keeps_a_short_name(self):
        # Rule 5: a started line that maps to no row is named by its first line, cut to a fixed length.
        name = self.launched_at("01:00").name
        self.assertTrue(name)
        self.assertLessEqual(len(name), 80)
        self.assertTrue(SWEEP_PROMPT.splitlines()[0].startswith(name), name)


# #204: a launch names its task by the text before ":"; that name outranks a later row that merely shares,
# or also carries, the same issue ref.
RENAMED_ROW_TRACKER = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Fix !182 review findings | Patch the null check flagged in review | w1 | running 00:20 | 2026-09-26 |  | S | build | triage | !182 | c |
| Verify !182 third pass | Verify !182 third pass | w2 | running 00:40 | 2026-09-26 |  | S | build | triage | !182 | c |
"""
THIRD_ROW_SHARES_REF_TRACKER = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Review !182 first pass | Review 182 | w1 | running 00:10 | 2026-09-26 |  | S | build | triage | !182 | c |
| Verify !182 third pass | Confirm the deploy is clean | w2 | running 00:40 | 2026-09-26 |  | S | build | triage | !182 | c |
| Ship !182 release notes | Ship !182 release notes | w3 | running 01:00 | 2026-09-26 |  | S | build | triage | !182 | c |
"""


class LaunchNamesItsRowTest(unittest.TestCase):
    """A launch lands on the row its text names, even once that row's item cell has changed and a later row
    shares, or mentions, the same issue ref (#204)."""

    def test_a_launch_lands_on_its_renamed_row_not_a_later_row_sharing_the_ref(self):
        tasks = rb.parse_tracker(RENAMED_ROW_TRACKER).tasks
        text = "Fix !182 review findings: patch the null check the second reviewer flagged"
        task, name = fc.launch_row(text, tasks)
        self.assertEqual(name, "Fix !182 review findings")
        self.assertEqual(task.item, "Patch the null check flagged in review")

    def test_a_launch_lands_on_its_named_row_not_a_third_row_that_mentions_the_ref(self):
        tasks = rb.parse_tracker(THIRD_ROW_SHARES_REF_TRACKER).tasks
        text = "Verify !182 third pass: confirm the rollback plan is documented"
        task, name = fc.launch_row(text, tasks)
        self.assertEqual(name, "Verify !182 third pass")
        self.assertEqual(task.item, "Confirm the deploy is clean")


class ManyLaunchesTest(unittest.TestCase):
    """A board over a tracker with hundreds of launches maps each to its row within seconds (#184)."""

    N = 400

    @classmethod
    def setUpClass(cls):
        text, cls.expected = many_launched_tracker(cls.N)
        log, tasks = fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks
        start = time.perf_counter()
        cls.got = fc.build(log, tasks, LANES, set(), {}, NOW)
        cls.took = time.perf_counter() - start

    def test_the_chart_builds_within_seconds(self):
        self.assertLess(self.took, 5, f"{self.N} launches took {self.took:.1f}s")

    def test_every_launch_lands_on_its_own_row(self):
        # Half map by item prefix, half only by issue ref; each draws on its own row with that row's ref and name.
        got = {row.name: (row.ref, [g.start for g in row.segments]) for _, row in self.got}
        self.assertEqual(got, {name: (ref, [at(TODAY, hhmm)]) for name, (ref, hhmm, _) in self.expected.items()})


def titles(built: list[tuple[str, gantt.Row]], name: str) -> list[str]:
    """The rendered bar titles on the Flow rows named `name`, across both charts."""
    html = fc.section(built, NOW)
    return [t for row in re.findall(r'<div class="gantt-row">.*?</div>', html) if f'<span class="gantt-name">{name}</span>' in row
            for t in re.findall(r' title="([^"]*)"', row)]


class OwnerTest(unittest.TestCase):
    """Each bar names who held its stage (#186)."""

    def test_a_bar_after_a_started_line_names_its_worker(self):
        # "A bar that follows a one-shot started line carries that worker in its title, e.g.
        # `implement 20:05–21:35 · W · done`."
        text = OneShotTest().tracker()  # w1 launched Audit headers at 00:20; its owner cell is now Robin
        built = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, set(), {}, NOW)
        implement = [t for t in titles(built, "Audit headers") if t.startswith("implement ")]
        self.assertTrue(implement)
        for title in implement:
            self.assertIn(" · w1 · ", title)
        # A launch that maps to no tracker row has no owner cell; its worker still names the bar.
        text = launched_tracker()
        built = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, set(), {}, NOW)
        sweep = next(r for _, r in built if any(g.start == at(TODAY, "01:00") for g in r.segments))
        got = titles(built, sweep.name)
        self.assertTrue(got)
        for title in got:
            self.assertIn(" · impl-3 · ", title)

    def test_a_bar_with_no_running_launch_names_the_owner_cell(self):
        # "A bar with no running launch carries the task's tracker owner cell in its title"
        for name, owner in (("Upload size limit", "impl-1"), ("Cache warmup", "Robin")):
            with self.subTest(task=name):
                done = [t for t in titles(list(rows().values()), name) if t.endswith(" · done")]
                self.assertTrue(done)
                for title in done:
                    self.assertIn(f" · {owner} · ", title)
        # w1 completed Audit headers at 01:30; the pr bar after it has no running launch.
        text = OneShotTest().tracker()
        built = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, set(), {}, NOW)
        pr = [t for t in titles(built, "Audit headers") if t.startswith("pr ")]
        self.assertTrue(pr)
        for title in pr:
            self.assertIn(" · Robin · ", title)


def bar_titles(row: gantt.Row, kind: str) -> set[str]:
    """The titles of `row`'s bars of `kind`; fails when it has none."""
    got = {g.title for g in row.segments if g.kind == kind}
    assert got, f"{row.name} has no {kind} bar"
    return got


UPLOAD_LAUNCH = "- 01:30 one-shot w7 started: claude, worktree `trees/up`, task Cap uploads"


class ForecastOwnerTest(unittest.TestCase):
    """Forecast and hold bars name who holds the task, as done bars do (#192)."""

    def test_a_forecast_bar_names_the_worker_running_now(self):
        # "A forecast bar's title names the worker whose launch is running now, else the task's owner cell"
        tracker = tw.append_log(TODAY_TRACKER, UPLOAD_LAUNCH)  # Upload size limit's owner cell is impl-1
        _, row = rows({"Cap uploads": NOW + 4 * H}, tracker=tracker)["Upload size limit"]
        self.assertEqual(bar_titles(row, "forecast"), {"w7"})

    def test_a_forecast_bar_with_no_launch_running_now_names_the_owner_cell(self):
        ended = tw.append_log(tw.append_log(TODAY_TRACKER, UPLOAD_LAUNCH), "- 01:50 one-shot w7: completed; awaiting integration")
        for label, tracker in (("no launch", TODAY_TRACKER), ("a launch that ended", ended)):
            with self.subTest(label):
                _, row = rows({"Cap uploads": NOW + 4 * H}, tracker=tracker)["Upload size limit"]
                self.assertEqual(bar_titles(row, "forecast"), {"impl-1"})

    def test_a_hold_bar_names_the_same_owner(self):
        # "A hold bar's title names the same owner"
        self.assertEqual(bar_titles(rows()["Cache warmup"][1], "hold"), {"Robin"})
        tracker = tw.append_log(TODAY_TRACKER, "- 01:45 one-shot w8 started: claude, worktree `trees/warm`, task Warm the pool")
        self.assertEqual(bar_titles(rows(tracker=tracker)["Cache warmup"][1], "hold"), {"w8"})

    def test_a_queued_tasks_forecast_names_its_owner_cell(self):
        tracker = QUEUED.replace("| Export format | Export CSV | unassigned |", "| Export format | Export CSV | Robin |")
        got = rows({"Cap uploads": NOW + 4 * H}, {"Export CSV": 5 * H}, 1, tracker)
        self.assertEqual(bar_titles(got["Export format"][1], "forecast"), {"Robin"})

    def test_a_task_with_no_owner_has_no_owner_part(self):
        # "A task with no owner keeps bar titles with no owner part." The same rows with an owner cell name it.
        for owner in ("Robin", ""):
            tracker = (QUEUED.replace("| Export format | Export CSV | unassigned |", f"| Export format | Export CSV | {owner} |")
                       .replace("| Cap uploads | impl-1 |", f"| Cap uploads | {owner} |")
                       .replace("| Warm the pool | Robin |", f"| Warm the pool | {owner} |"))
            got = rows({"Cap uploads": NOW + 4 * H}, {"Export CSV": 5 * H}, 1, tracker)
            for name, kind in (("Upload size limit", "forecast"), ("Cache warmup", "hold"), ("Export format", "forecast")):
                with self.subTest(owner=owner, task=name):
                    self.assertEqual(bar_titles(got[name][1], kind), {owner})


class UnassignedOwnerTest(unittest.TestCase):
    """An owner cell of `unassigned`, in any case, is the tracker's word for no owner (#193)."""

    def built(self, owner: str) -> dict[str, tuple[str, gantt.Row]]:
        """TODAY_TRACKER's rows with Cache warmup's owner cell set to `owner`, whitespace kept."""
        tasks = [replace(t, owner=owner) if t.name == "Cache warmup" else t for t in rb.parse_tracker(TODAY_TRACKER).tasks]
        return {row.name: (status, row) for status, row in fc.build(moves(), tasks, LANES, {"Cache warmup"}, {}, NOW)}

    def test_an_unassigned_owner_cell_draws_no_owner_part(self):
        # "A Flow bar of a task whose owner cell is `unassigned` (any case) has no owner part in its title."
        # "Any other owner cell still shows as written."
        for owner, want in (("unassigned", ""), ("Unassigned", ""), (" UNASSIGNED ", ""), ("unassigned-bot", "unassigned-bot")):
            with self.subTest(owner=owner):
                _, row = self.built(owner)["Cache warmup"]
                self.assertEqual(bar_titles(row, "done"), {want})
                self.assertEqual(bar_titles(row, "hold"), {want})
                queued = QUEUED.replace("| Export format | Export CSV | unassigned |", f"| Export format | Export CSV | {owner.strip()} |")
                got = rows({"Cap uploads": NOW + 4 * H}, {"Export CSV": 5 * H}, 1, queued)
                self.assertEqual(bar_titles(got["Export format"][1], "forecast"), {want})


def merged(issue: str, hhmm: str, n: int = 1) -> bs.Change:
    """A change the forge reports merged at `hhmm` today, closing `issue`, stamped in UTC as the forge cache holds it."""
    return bs.Change(f"!{n}", "u", "t", "merged", False, "passed", True, 1, at(TODAY, hhmm).astimezone(timezone.utc),
                     "main", (), (issue,))


def board(tracker: str, changes: list[bs.Change], stage_log: tuple = ((YESTERDAY, YESTERDAY_TRACKER),)) -> tuple[str, dict[str, tuple[str, gantt.Row]]]:
    """The rendered board, and the Flow rows it drew by name (as render handed them to flow_chart.section)."""
    seen, real = [], fc.section
    with mock.patch.object(rb.flow_chart, "section", lambda rows, now: seen.append(rows) or real(rows, now)):
        html = rb.render(tracker, BoardTest().cfg(), NOW, lanes=LANES, tracker_day=TODAY, kanban=KANBAN,
                         stage_log=list(stage_log), sources=bs.Sources({}, {}, {c.ref: c for c in changes}, (), {}))
    return html, {row.name: (status, row) for status, row in seen[0]}


class ForgeMergeTest(unittest.TestCase):
    """A row ends at the forge's merge time for its task's change (#186)."""

    def test_a_forge_merge_ends_the_row_with_no_stage_line_to_main(self):
        # "A task whose change the forge reports merged notes `merged HH:MM` at that time, with no bar past it and no
        # forecast or hold."
        html, got = board(TODAY_TRACKER, [merged("#109", "01:15")])
        status, row = got["Upload size limit"]
        self.assertEqual((status, row.note), ("merged", "merged 01:15"))
        self.assertIn('<span class="gantt-name">Upload size limit</span>', html)
        self.assertIn('<span class="gantt-note">merged 01:15</span>', html)
        self.assertTrue(row.segments)
        for g in row.segments:
            self.assertLessEqual(g.end, at(TODAY, "01:15"), g)
            self.assertNotIn(g.kind, ("forecast", "hold"), g)

    def test_a_held_task_whose_change_merged_is_merged(self):
        # "A held task whose change merged shows as merged, not held."
        html, got = board(TODAY_TRACKER, [merged("#111", "01:50")])
        status, row = got["Cache warmup"]
        self.assertEqual((status, row.note), ("merged", "merged 01:50"))
        self.assertNotIn("hold", [g.kind for g in row.segments])
        self.assertIn('<span class="gantt-note">merged 01:50</span>', html)

    def test_the_forge_merge_time_beats_the_stage_line(self):
        # "When a stage line to main and the forge disagree on the merge time, the forge time wins."
        tracker = tw.append_log(TODAY_TRACKER, "- 01:30 stage: Upload size limit → main")
        for forge in ("01:15", "01:45"):
            with self.subTest(forge=forge):
                html, got = board(tracker, [merged("#109", forge)])
                self.assertEqual(got["Upload size limit"][1].note, f"merged {forge}")
                self.assertNotIn('<span class="gantt-note">merged 01:30</span>', html)


class ManyMergedTest(unittest.TestCase):
    """A board over hundreds of launched tasks, each with a merged change, renders within seconds (#186)."""

    N = 400

    @classmethod
    def setUpClass(cls):
        text, cls.expected = many_launched_tracker(cls.N)
        changes = [merged(ref, "02:05", i) for i, (ref, _, _) in enumerate(cls.expected.values())]
        start = time.perf_counter()
        cls.html, cls.got = board(text, changes, ())
        cls.took = time.perf_counter() - start

    def test_the_board_renders_within_seconds(self):
        # "A build over 400 tasks, each with launches and a merged change, finishes within 5 seconds."
        self.assertLess(self.took, 5, f"{self.N} merged launches took {self.took:.1f}s")

    def test_every_row_ends_at_its_merge(self):
        # The timing covers the merge join: "A task whose change the forge reports merged notes `merged HH:MM`".
        self.assertEqual({name: row.note for name, (_, row) in self.got.items()},
                         {name: "merged 02:05" for name in self.expected})


if __name__ == "__main__":
    unittest.main()
