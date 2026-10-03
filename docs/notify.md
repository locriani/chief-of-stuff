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

- `chief-of-stuff notify --root . ensure` installs and loads one macOS user LaunchAgent for the workspace when notifications are enabled. It reuses a matching job and replaces one pinned to another release. The coordinator launcher runs this before launching Claude, Codex, Cursor, or Antigravity. A direct plugin launch can run it at startup or resume. An off adapter creates no service or queue.
- `chief-of-stuff notify --root . status` prints TOON with `running`, `release`, `last_success`, `error`, and, after reconciliation, `disabled`. Running is verified through the service lock, not a potentially reused PID. A running service with an error keeps retrying; a stopped service may retain its last success and error.
- `chief-of-stuff notify --root . stop` unloads the job and removes its login registration. It stays stopped until the next `ensure`, including the one performed by a coordinator launch.
- `chief-of-stuff notify --root . serve` runs in the foreground, suitable for a supervisor on another platform. It checks input fingerprints every five seconds, syncs on changes, and performs a full reconciliation every minute and at startup. Inputs include `CLAUDE.md`, its settings file, and today's and earlier daily trackers. Midnight and clock changes trigger reconciliation in the workspace timezone. A malformed input leaves the queue unchanged and is retried; an existing service with notifications switched off continues checking for re-enablement without writing the queue.
- `notify.py --root . sync` performs the same reconciliation once for diagnostics: it writes future deadline warnings and the two recurring day rows. It carries future decisions from earlier trackers, removes obsolete future warnings, leaves past warnings for the user to tap, and retimes changed day rows. Missing today's tracker is fine. An unchanged sync does not rewrite the queue. Agents no longer run this after deadline edits.
- `notify.py --root . add --kind awaiting|stopped|gone --what "<a few words>" [--message M] [--when now|YYYY-MM-DD HH:MM]` adds one event. A time already passed is refused, because a past one-shot row fires at once and then resurfaces every 15 minutes until someone taps it.

Titles are built by the script, never typed: `⏰ <deadline> in <offset> (<Day> YYYY-MM-DD HH:MM)`, `🙋 Awaiting you — <what> (HH:MM)`, `🛑 Session stopped — <session> (HH:MM)`, `👻 Session gone — <session> (HH:MM)`, `☀️ Open the day`, `🌙 Close the day`. The title is the duplicate key: md-notify's Snooze rewrites a row's time and Complete and Ignore move it to another section, so the title is the only thing that survives all three. A title anywhere in the file is never added again. Sync updates matching old warning titles in place, preserving snoozes and parked rows; the full due date prevents a previous week's warning from hiding a new one. A Decisions row with no precise time creates no warning and does not cancel an earlier precise decision.

## The queue file

md-notify's parser (MNCore `NotificationSchedule.parse`) decides the format, and `evals/test_notify.py` ports it line for line to check every row the script writes:

- `| When (CT) | Title | Message | Open |`, with `When` as `YYYY-MM-DD HH:MM` in local time. Recurring rows sit under `## Recurring` with `daily HH:MM` or `<mon..sun> HH:MM`.
- A line containing `---` is read as a separator and one containing `When (CT)` as a header, wherever they occur, and `|` splits cells. The script replaces `|` with `/` and a run of three or more `-` with `—`, and collapses newlines.
- Rows under `## Completed` or `## Ignored` never fire. The app moves rows there when a banner is tapped.
- Every chief-of-stuff `add` and `sync` holds an exclusive POSIX `flock` on the persistent sibling `.<queue filename>.lock` across reading and atomic replacement. Lock acquisition times out after five seconds; no queue write occurs on timeout. Queue permissions are preserved. Never delete the lock file while either writer is running.
- md-notify must use that same lock across its acknowledgment read, transformation, and write, rereading after acquiring it. The companion `QueueWriter.update` / `FileQueueSource.update` change supplies that transaction. An older installed md-notify app does not participate in this protocol: atomic replacement alone cannot prevent acknowledgment loss. Rebuild and install the companion app before relying on concurrent acknowledgment preservation.

The service's status and logs live in `.chief-of-stuff/notify-status.json` and `.chief-of-stuff/notify-service.log`. The LaunchAgent lives in `~/Library/LaunchAgents/com.chief-of-stuff.notify.<workspace hash>.plist`, uses absolute executable paths, and stays pinned to the installed release selected by `ensure`. It runs in the logged-in user's GUI session and restarts after failure; it does not run while the Mac is asleep. On waking, reconciliation repairs future warnings without generating missed past ones. Nothing opens a browser or app. The md-notify queue format uses local wall-clock times, so its Mac timezone must match the workspace timezone.

## Installing md-notify

From its README: `scripts/build-number.sh`, `xcodegen generate`, `xcodebuild -project MarkdownNotify.xcodeproj -scheme MarkdownNotify -configuration Release build` (signed with the Illideri Studios Developer ID), copy the app to `~/Applications/`, then point it at the queue and open it:

```
defaults write com.illideri.MarkdownNotify NotificationsFilePaths -array <workspace>/notifications/NOTIFICATIONS.md
open ~/Applications/MarkdownNotify.app
```

The list replaces md-notify's default, so a second queue is added to the array rather than written alone. The app asks for notification permission on first launch and registers itself as a Login Item; restart it after changing the list.
