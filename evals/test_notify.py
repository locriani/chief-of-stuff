"""Unit tests for scripts/notify.py: the coordinator's notifications, written as an md-notify queue.

Zach, 2026-09-22 22:20: "consider integration with my md-notify repo for initial setup of notifications -
with a note that we will be plugging this out later and integrating with my todo tooling instead". He asked
for four kinds: awaiting you, deadline warnings, day open and close, a session stopped or gone.

md-notify's parser is ported below line for line, so every row the script writes is checked the way the app
will read it. No app, no notification centre and no network are involved.
"""

import io
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import notify as nt  # noqa: E402

CT = ZoneInfo("America/Chicago")
NOW = datetime(2026, 9, 23, 14, 0, tzinfo=CT)
QUEUE = "notes/NOTIFICATIONS.md"

CLAUDE = """# Workspace

## Coordinator

- User: Robin
- Daily log dir: `daily/`
- Tracker: `daily/<date>-tracker.md`
- Timezone: America/Chicago
- Deadlines:
  - Early: 2026-09-23 23:59
  - Final: 2026-09-27 12:00
  - Past: 2026-09-20 12:00
- Board: Artifact tool; URL https://claude.ai/artifact/board-7
- Settings: `chief-of-stuff.toml`
"""

TOML = f'[notify]\nadapter = "md-notify"\nqueue = "{QUEUE}"\n'


def tracker(*decisions: tuple[str, str]) -> str:
    rows = "".join(f"| {when} | {item} | recorded |\n" for when, item in decisions)
    return "## Decisions\n\n| time | item | source |\n|---|---|---|\n" + rows


def md_notify_entries(text: str) -> list[tuple[str, str]]:
    """`NotificationSchedule.parse`, ported: (when, title) for every row md-notify would consider firing."""
    out, excluded = [], False
    for line in text.split("\n"):
        trimmed = line.strip(" ")
        if trimmed.startswith("#"):
            excluded = trimmed.lstrip("#").strip(" ").lower() in ("completed", "ignored")
            continue
        if excluded or not line.startswith("|") or "---" in line or "When (CT)" in line:
            continue
        body = line[1:]
        body = body[:-1] if body.endswith("|") else body
        cells = [c.strip(" ") for c in body.split("|")]
        when = cells[0] if cells else ""
        title = cells[1] if len(cells) > 1 else ""
        if not when:
            continue
        try:
            datetime.strptime(when, "%Y-%m-%d %H:%M")
            out.append((when, title))
            continue
        except ValueError:
            pass
        if re.fullmatch(r"(?i)(daily|mon|tue|wed|thu|fri|sat|sun)\s+([01]?\d|2[0-3]):[0-5]\d", when):
            out.append((when, title))
    return out


class Workspace:
    def __init__(self, claude: str = CLAUDE, toml: str | None = TOML):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "CLAUDE.md").write_text(claude)
        if toml is not None:
            (self.root / "chief-of-stuff.toml").write_text(toml)

    @property
    def queue(self) -> Path:
        return self.root / QUEUE

    def run(self, *argv: str, now: datetime = NOW) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = nt.main(["--root", str(self.root), *argv], now=now)
        return code, buf.getvalue()


class NotifyTest(unittest.TestCase):
    def ws(self, **kw) -> Workspace:
        w = Workspace(**kw)
        self.addCleanup(w.tmp.cleanup)
        return w

    def test_first_sync_creates_the_queue(self):
        w = self.ws()
        code, out = w.run("sync")
        self.assertEqual(code, 0, out)
        text = w.queue.read_text()
        self.assertIn("md-notify", text)
        self.assertIn("| When (CT) | Title | Message | Open |", text)
        self.assertIn("## Recurring", text)
        self.assertEqual(sorted(md_notify_entries(text)), sorted([
            ("2026-09-23 20:59", "⏰ Early in 3h (Wed 2026-09-23 23:59)"),
            ("2026-09-23 22:59", "⏰ Early in 1h (Wed 2026-09-23 23:59)"),
            ("2026-09-26 12:00", "⏰ Final in 24h (Sun 2026-09-27 12:00)"),
            ("2026-09-27 09:00", "⏰ Final in 3h (Sun 2026-09-27 12:00)"),
            ("2026-09-27 11:00", "⏰ Final in 1h (Sun 2026-09-27 12:00)"),
            ("daily 06:00", "☀️ Open the day"),
            ("daily 22:00", "🌙 Close the day"),
        ]))
        self.assertIn("https://claude.ai/artifact/board-7", text)

    def test_a_warning_already_past_is_never_written(self):
        """A one-shot row resurfaces every 15 minutes until tapped, so a past one would nag for nothing."""
        w = self.ws()
        w.run("sync")
        for when, _ in md_notify_entries(w.queue.read_text()):
            if not when.startswith("daily"):
                self.assertGreater(datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=CT), NOW)

    def test_second_sync_changes_nothing(self):
        w = self.ws()
        w.run("sync")
        before = w.queue.read_bytes()
        code, out = w.run("sync")
        self.assertEqual((code, out.strip()), (0, "notify: nothing to change"))
        self.assertEqual(w.queue.read_bytes(), before)

    def test_add_goes_in_the_active_table(self):
        w = self.ws()
        w.run("sync")
        code, out = w.run("add", "--kind", "awaiting", "--what", "CIMP 0.21.0")
        self.assertEqual(code, 0, out)
        lines = w.queue.read_text().split("\n")
        row = next(i for i, x in enumerate(lines) if "Awaiting you" in x)
        self.assertLess(row, lines.index("## Recurring"))
        self.assertIn(("2026-09-23 14:00", "🙋 Awaiting you — CIMP 0.21.0 (14:00)"), md_notify_entries("\n".join(lines)))

    def test_kinds(self):
        for kind, head in (("awaiting", "🙋 Awaiting you"), ("stopped", "🛑 Session stopped"), ("gone", "👻 Session gone")):
            with self.subTest(kind=kind):
                self.assertEqual(nt.event_title(kind, "impl02", NOW), f"{head} — impl02 (14:00)")

    def test_a_title_anywhere_is_not_added_again(self):
        """Snooze rewrites When, and Complete and Ignore park a row: the Title is the only stable key."""
        title = "🙋 Awaiting you — CIMP 0.21.0 (14:00)"
        for section, row in (("## Completed", f"| 2026-09-23 14:00 | {title} | m | |"),
                             ("## Ignored", f"| 2026-09-23 14:00 | {title} | m | |"),
                             ("", f"| 2026-09-23 14:30 | {title} | m | | 1 |")):
            with self.subTest(section=section or "snoozed"):
                w = self.ws()
                w.queue.parent.mkdir(parents=True)
                header = "| When (CT) | Title | Message | Open | Snoozes |\n|---|---|---|---|---|\n"
                w.queue.write_text(f"# Q\n\n{header}{row if not section else ''}\n\n{section}\n{header if section else ''}{row if section else ''}\n")
                before = w.queue.read_bytes()
                code, out = w.run("add", "--kind", "awaiting", "--what", "CIMP 0.21.0")
                self.assertEqual(code, 0)
                self.assertIn("already there", out)
                self.assertEqual(w.queue.read_bytes(), before)

    def test_cells_are_made_safe_for_the_parser(self):
        w = self.ws()
        w.run("add", "--kind", "awaiting", "--what", "a | b\nc --- d When (CT) e", "--message", "x|y\n---z")
        entries = [t for _, t in md_notify_entries(w.queue.read_text()) if "Awaiting" in t]
        self.assertEqual(len(entries), 1, w.queue.read_text())
        self.assertNotIn("|", entries[0])
        self.assertNotIn("When (CT)", entries[0])

    def test_everything_else_is_preserved(self):
        w = self.ws()
        w.queue.parent.mkdir(parents=True)
        original = ("# Mine\n\nSome prose I wrote.\n\n| When (CT) | Title | Message | Open | Snoozes |\n|---|---|---|---|---|\n"
                    "| 2026-09-24 09:00 | Dentist | go | | 2 |\n\n## Recurring\n| Every | Title | Message | Open |\n|---|---|---|---|\n"
                    "| mon 09:30 | Standup | join | |\n\n## Completed\n| When (CT) | Title | Message | Open |\n|---|---|---|---|\n"
                    "| 2026-09-20 09:00 | Old | done | |\n")
        w.queue.write_text(original)
        w.run("add", "--kind", "gone", "--what", "impl02")
        after = w.queue.read_text().split("\n")
        added = [x for x in after if "Session gone" in x]
        self.assertEqual(len(added), 1)
        self.assertEqual([x for x in after if x not in added], original.split("\n"))
        self.assertEqual(after.index(added[0]), original.split("\n").index("| 2026-09-24 09:00 | Dentist | go | | 2 |") + 1)

    def test_a_moved_deadline_replaces_its_future_rows(self):
        w = self.ws()
        w.run("sync")
        (w.root / "CLAUDE.md").write_text(CLAUDE.replace("Final: 2026-09-27 12:00", "Final: 2026-09-27 18:00"))
        code, out = w.run("sync")
        titles = [t for _, t in md_notify_entries(w.queue.read_text())]
        self.assertFalse([t for t in titles if "2026-09-27 12:00)" in t], out)
        self.assertEqual(len([t for t in titles if "2026-09-27 18:00)" in t]), 3, out)
        self.assertEqual(len([t for t in titles if t.startswith("⏰ Early")]), 2)

    def test_a_removed_deadline_loses_future_rows_and_keeps_past_ones(self):
        w = self.ws()
        w.run("sync")
        text = w.queue.read_text().replace("| When (CT) | Title | Message | Open |\n|---|---|---|---|\n",
                                           "| When (CT) | Title | Message | Open |\n|---|---|---|---|\n| 2026-09-23 12:59 | ⏰ Early in 11h (Wed 23:59) | Due | |\n", 1)
        w.queue.write_text(text)
        (w.root / "CLAUDE.md").write_text(CLAUDE.replace("  - Early: 2026-09-23 23:59\n", ""))
        w.run("sync")
        titles = [t for _, t in md_notify_entries(w.queue.read_text())]
        self.assertEqual([t for t in titles if t.startswith("⏰ Early")], ["⏰ Early in 11h (Wed 23:59)"])

    def test_today_decision_adds_warnings_and_moving_it_removes_old_future_rows(self):
        w = self.ws()
        path = w.root / "daily/2026-09-23-tracker.md"
        path.parent.mkdir()
        path.write_text(tracker(("14:01", "deadline: Review 2026-09-27 16:00")))
        w.run("sync")
        titles = [title for _, title in md_notify_entries(w.queue.read_text())]
        self.assertEqual(len([title for title in titles if "Review" in title and "2026-09-27 16:00" in title]), 3)
        path.write_text(tracker(("14:01", "deadline: Review 2026-09-27 16:00"),
                                ("14:02", "deadline: Review 2026-09-27 18:00")))
        w.run("sync")
        titles = [title for _, title in md_notify_entries(w.queue.read_text())]
        self.assertFalse([title for title in titles if "Review" in title and "16:00" in title])
        self.assertEqual(len([title for title in titles if "Review" in title and "18:00" in title]), 3)
        before = w.queue.read_bytes()
        self.assertEqual(w.run("sync")[1].strip(), "notify: nothing to change")
        self.assertEqual(w.queue.read_bytes(), before)

    def test_yesterday_decision_carries_forward_and_today_overrides_it(self):
        w = self.ws()
        directory = w.root / "daily"
        directory.mkdir()
        (directory / "2026-09-22-tracker.md").write_text(
            tracker(("17:00", "deadline: Review 2026-09-27 16:00")))
        w.run("sync")  # today's tracker does not exist yet
        self.assertEqual(sum("Review" in title for _, title in md_notify_entries(w.queue.read_text())), 3)
        (directory / "2026-09-23-tracker.md").write_text(
            tracker(("14:01", "deadline: Review 2026-09-27 18:00")))
        w.run("sync")
        titles = [title for _, title in md_notify_entries(w.queue.read_text()) if "Review" in title]
        self.assertEqual(len(titles), 3)
        self.assertTrue(all("2026-09-27 18:00" in title for title in titles))

    def test_no_time_decision_adds_no_warning(self):
        w = self.ws()
        path = w.root / "daily/2026-09-23-tracker.md"
        path.parent.mkdir()
        path.write_text(tracker(("14:01", "deadline: Exam, Sat 2026-09-26 (no time given)")))
        w.run("sync")
        self.assertNotIn("Exam", w.queue.read_text())

    def test_same_weekday_time_next_week_gets_a_new_warning(self):
        w = self.ws()
        w.run("sync")
        (w.root / "CLAUDE.md").write_text(CLAUDE.replace("Final: 2026-09-27 12:00", "Final: 2026-10-04 12:00"))
        w.run("sync")
        titles = [title for _, title in md_notify_entries(w.queue.read_text()) if "Final" in title]
        self.assertEqual(len(titles), 3)
        self.assertTrue(all("2026-10-04" in title for title in titles))

    def test_legacy_warning_titles_migrate_without_losing_snooze_or_parked_state(self):
        w = self.ws()
        w.queue.parent.mkdir(parents=True)
        w.queue.write_text(
            "# Mine\n\n| When (CT) | Title | Message | Open | Snoozes |\n|---|---|---|---|---|\n"
            "| 2026-09-23 23:30 | ⏰ Early in 3h (Wed 23:59) | Due 2026-09-23 23:59 | | 1 |\n"
            "\n## Completed\n| When (CT) | Title | Message | Open |\n|---|---|---|---|\n"
            "| 2026-09-23 22:59 | ⏰ Early in 1h (Wed 23:59) | Due 2026-09-23 23:59 | |\n")
        w.run("sync")
        text = w.queue.read_text()
        self.assertIn("| 2026-09-23 23:30 | ⏰ Early in 3h (Wed 2026-09-23 23:59) | Due 2026-09-23 23:59 | | 1 |", text)
        self.assertIn("## Completed", text)
        self.assertIn("| 2026-09-23 22:59 | ⏰ Early in 1h (Wed 2026-09-23 23:59) | Due 2026-09-23 23:59 | |", text)
        self.assertEqual(sum("Early in 1h" in title for _, title in md_notify_entries(text)), 0)

    def test_parked_legacy_warning_from_previous_week_does_not_hide_new_warning(self):
        w = self.ws()
        w.queue.parent.mkdir(parents=True)
        w.queue.write_text(
            "# Mine\n\n## Completed\n| When (CT) | Title | Message | Open |\n|---|---|---|---|\n"
            "| 2026-09-16 20:59 | ⏰ Early in 3h (Wed 23:59) | Due 2026-09-16 23:59 | |\n")
        w.run("sync")
        titles = [title for _, title in md_notify_entries(w.queue.read_text())]
        self.assertIn("⏰ Early in 3h (Wed 2026-09-23 23:59)", titles)

    def test_day_rows_follow_the_settings(self):
        w = self.ws()
        w.run("sync")
        (w.root / "chief-of-stuff.toml").write_text(TOML + 'day_open = "07:15"\n')
        code, out = w.run("sync")
        entries = md_notify_entries(w.queue.read_text())
        self.assertIn(("daily 07:15", "☀️ Open the day"), entries)
        self.assertNotIn(("daily 06:00", "☀️ Open the day"), entries)
        self.assertIn(("daily 22:00", "🌙 Close the day"), entries)

    def test_a_hand_written_queue_gains_a_recurring_table(self):
        w = self.ws()
        w.queue.parent.mkdir(parents=True)
        w.queue.write_text("# Mine\n\n| When (CT) | Title | Message | Open |\n|---|---|---|---|\n| 2026-09-24 09:00 | Dentist | go | |\n")
        w.run("sync")
        text = w.queue.read_text()
        self.assertTrue(text.startswith("# Mine\n\n| When (CT) | Title | Message | Open |\n|---|---|---|---|\n| 2026-09-24 09:00 | Dentist | go | |\n"), text)
        entries = md_notify_entries(text)
        self.assertIn(("daily 06:00", "☀️ Open the day"), entries)
        self.assertEqual(len([e for e in entries if e[1].startswith("⏰")]), 5)
        self.assertLess(text.index("⏰"), text.index("## Recurring"))

    def test_a_later_when(self):
        w = self.ws()
        w.run("add", "--kind", "awaiting", "--what", "x", "--when", "2026-09-23 18:30")
        self.assertIn(("2026-09-23 18:30", "🙋 Awaiting you — x (14:00)"), md_notify_entries(w.queue.read_text()))

    def test_a_past_when_is_refused(self):
        w = self.ws()
        code, _ = w.run("add", "--kind", "awaiting", "--what", "x", "--when", "2026-09-23 09:00")
        self.assertEqual(code, 2)
        self.assertFalse(w.queue.exists())

    def test_off(self):
        for toml in (None, '[notify]\n', '[notify]\nadapter = "off"\n'):
            with self.subTest(toml=toml):
                w = self.ws(toml=toml)
                for argv in (("sync",), ("add", "--kind", "awaiting", "--what", "x")):
                    code, out = w.run(*argv)
                    self.assertEqual((code, out.strip()), (0, "notify: off"))
                self.assertFalse(w.queue.exists())

    def test_no_settings_line_is_off(self):
        w = self.ws(claude=CLAUDE.replace("- Settings: `chief-of-stuff.toml`\n", ""))
        self.assertEqual(w.run("sync")[1].strip(), "notify: off")

    def test_bad_settings_exit_two(self):
        w = self.ws(toml='[notify]\nday_open = "6am"\n')
        self.assertEqual(w.run("sync")[0], 2)

    def test_the_write_is_atomic(self):
        """One os.replace from a temp file beside the queue: md-notify never reads half a file."""
        w = self.ws()
        calls = []
        real = nt.os.replace

        def spy(src, dst):
            calls.append((Path(src).resolve().parent, Path(dst).resolve()))
            return real(src, dst)

        nt.os.replace = spy
        self.addCleanup(setattr, nt.os, "replace", real)
        w.run("sync")
        self.assertEqual(calls, [(w.queue.parent.resolve(), w.queue.resolve())])


if __name__ == "__main__":
    unittest.main()
