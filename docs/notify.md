# Notifications

The coordinator can notify the user about a move awaiting them, a coming deadline, the day's open and close, or a stopped or gone session. `scripts/notify.py` writes only when workspace settings select an adapter.

## Optional md-notify adapter

The md-notify adapter writes a markdown queue for the macOS app to read and display as banners with Open, Snooze, Complete, and Ignore actions.

Another adapter can implement `add` and `sync` beside `MdNotifyQueue` and be selected in settings. Other tools should not rewrite the md-notify queue because they could erase parked rows.

## Settings

The `## Coordinator` block names the file: `- Settings: chief-of-stuff.toml`, relative to the workspace root. Notifications remain off without an explicit `adapter = "md-notify"`, including when `[notify]` is present but empty.

```toml
[notify]
adapter = "md-notify"                          # or "off"
queue = "notifications/NOTIFICATIONS.md"       # under the workspace root
day_open = "06:00"
day_close = "22:00"
deadline_warnings = ["24h", "3h", "1h"]        # Nh or Nm before each deadline
```

A missing key takes the default shown. A value that is there and cannot be read (an unknown adapter, a time that is not `HH:MM`, an offset that is not `Nh` or `Nm`, a queue outside the root) stops the script with exit 2 and never falls back to a guess.

## Commands

- `notify.py --root . sync` writes a warning for each configured or precise `deadline:` Decisions-row deadline and each offset that is still ahead, and the two day rows under `## Recurring`. It reads today's tracker and carries future decisions forward from earlier daily trackers, using the same resolver as the board. A missing today's tracker is fine during Open the day. It removes obsolete future warnings when an effective deadline moves or disappears, leaves past warnings for the user to tap, and retimes a day row when the settings change. A second run with nothing changed writes nothing. Run sync after writing or changing a precise deadline decision.
- `notify.py --root . add --kind awaiting|stopped|gone --what "<a few words>" [--message M] [--when now|YYYY-MM-DD HH:MM]` adds one event. A time already passed is refused, because a past one-shot row fires at once and then resurfaces every 15 minutes until someone taps it.

Titles are built by the script, never typed: `⏰ <deadline> in <offset> (<Day> YYYY-MM-DD HH:MM)`, `🙋 Awaiting you — <what> (HH:MM)`, `🛑 Session stopped — <session> (HH:MM)`, `👻 Session gone — <session> (HH:MM)`, `☀️ Open the day`, `🌙 Close the day`. The title is the duplicate key: md-notify's Snooze rewrites a row's time and Complete and Ignore move it to another section, so the title is the only thing that survives all three. A title anywhere in the file is never added again. Sync updates matching old warning titles in place, preserving snoozes and parked rows; the full due date prevents a previous week's warning from hiding a new one. A Decisions row with no precise time creates no warning and does not cancel an earlier precise decision.

## The queue file

md-notify's parser (MNCore `NotificationSchedule.parse`) decides the format, and `evals/test_notify.py` ports it line for line to check every row the script writes:

- `| When (CT) | Title | Message | Open |`, with `When` as `YYYY-MM-DD HH:MM` in local time. Recurring rows sit under `## Recurring` with `daily HH:MM` or `<mon..sun> HH:MM`.
- A line containing `---` is read as a separator and one containing `When (CT)` as a header, wherever they occur, and `|` splits cells. The script replaces `|` with `/` and a run of three or more `-` with `—`, and collapses newlines.
- Rows under `## Completed` or `## Ignored` never fire. The app moves rows there when a banner is tapped.
- The app rewrites the file in place on a tap. The script writes with one `os.replace` from a temp file in the same directory, so neither side reads half a file. A tap that lands inside the script's own read-modify-write can still lose one of the two edits.

## Installing md-notify

From its README: `scripts/build-number.sh`, `xcodegen generate`, `xcodebuild -project MarkdownNotify.xcodeproj -scheme MarkdownNotify -configuration Release build` (signed with the Illideri Studios Developer ID), copy the app to `~/Applications/`, then point it at the queue and open it:

```
defaults write com.illideri.MarkdownNotify NotificationsFilePaths -array <workspace>/notifications/NOTIFICATIONS.md
open ~/Applications/MarkdownNotify.app
```

The list replaces md-notify's default, so a second queue is added to the array rather than written alone. The app asks for notification permission on first launch and registers itself as a Login Item; restart it after changing the list.
