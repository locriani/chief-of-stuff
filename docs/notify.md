# Notifications

The coordinator tells the user about four things: a move that is waiting on them, a deadline coming up, the day's open and close, and a session that stopped or is gone (Zach, 2026-09-22 22:20). It says them through `scripts/notify.py`, which writes to whatever notifier the settings name.

## Interim: md-notify, to be replaced by Todo

The adapter today is md-notify (`~/Developer/md-notify`, MarkdownNotify.app), a macOS background app that reads a markdown queue every minute and posts a banner with Open, Snooze 30 min, Complete and Ignore. Zach's words: "initial setup of notifications - with a note that we will be plugging this out later and integrating with my todo tooling instead at a future point."

Replacing it means one new class in `notify.py` beside `MdNotifyQueue`, with the same `add` and `sync`, and one new value for `adapter` in the settings. No caller changes. Until then, never point Todo's `compile` at the md-notify queue: it regenerates its whole output file and would erase every row md-notify has parked.

## Settings

The `## Coordinator` block names the file: `- Settings: chief-of-stuff.toml`, relative to the workspace root. No line, no file, or no `[notify]` table is notify off.

```toml
[notify]
adapter = "md-notify"                          # or "off"
queue = "Areas/notifications/NOTIFICATIONS.md" # under the workspace root
day_open = "06:00"
day_close = "22:00"
deadline_warnings = ["24h", "3h", "1h"]        # Nh or Nm before each deadline
```

A missing key takes the default shown. A value that is there and cannot be read (an unknown adapter, a time that is not `HH:MM`, an offset that is not `Nh` or `Nm`, a queue outside the root) stops the script with exit 2 and never falls back to a guess.

## Commands

- `notify.py --root . sync` writes a warning for each deadline and each offset that is still ahead, and the two day rows under `## Recurring`. It removes a future warning the block no longer asks for (a deadline removed or moved), leaves a past one for the user to tap, and retimes a day row when the settings change. A second run with nothing changed writes nothing.
- `notify.py --root . add --kind awaiting|stopped|gone --what "<a few words>" [--message M] [--when now|YYYY-MM-DD HH:MM]` adds one event. A time already passed is refused, because a past one-shot row fires at once and then resurfaces every 15 minutes until someone taps it.

Titles are built by the script, never typed: `⏰ <deadline> in <offset> (<Day> HH:MM)`, `🙋 Awaiting you — <what> (HH:MM)`, `🛑 Session stopped — <session> (HH:MM)`, `👻 Session gone — <session> (HH:MM)`, `☀️ Open the day`, `🌙 Close the day`. The title is the duplicate key: md-notify's Snooze rewrites a row's time and Complete and Ignore move it to another section, so the title is the only thing that survives all three. A title anywhere in the file is never added again.

## The queue file

md-notify's parser (MNCore `NotificationSchedule.parse`) decides the format, and `evals/test_notify.py` ports it line for line to check every row the script writes:

- `| When (CT) | Title | Message | Open |`, with `When` as `YYYY-MM-DD HH:MM` in local time. Recurring rows sit under `## Recurring` with `daily HH:MM` or `<mon..sun> HH:MM`.
- A line containing `---` is read as a separator and one containing `When (CT)` as a header, wherever they occur, and `|` splits cells. The script replaces `|` with `/` and a run of three or more `-` with `—`, and collapses newlines.
- Rows under `## Completed` or `## Ignored` never fire. The app moves rows there when a banner is tapped.
- The app rewrites the file in place on a tap. The script writes with one `os.replace` from a temp file in the same directory, so neither side reads half a file. A tap that lands inside the script's own read-modify-write can still lose one of the two edits.

## Installing md-notify

From its README: `scripts/build-number.sh`, `xcodegen generate`, `xcodebuild -project MarkdownNotify.xcodeproj -scheme MarkdownNotify -configuration Release build` (signed with the Illideri Studios Developer ID), copy the app to `~/Applications/`, then point it at the queue and open it:

```
defaults write com.illideri.MarkdownNotify NotificationsFilePaths -array <workspace>/Areas/notifications/NOTIFICATIONS.md
open ~/Applications/MarkdownNotify.app
```

The list replaces md-notify's default, so a second queue is added to the array rather than written alone. The app asks for notification permission on first launch and registers itself as a Login Item; restart it after changing the list.
