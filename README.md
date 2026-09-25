# chief-of-stuff

A daily coordinator for Claude Code, Codex CLI, Cursor CLI, and Antigravity CLI. The workspace keeps its existing `CLAUDE.md` `## Coordinator` block. The coordinator routes work, keeps the tracker and board current, and asks before every new session launch.

## Install and launch

From this checkout:

```sh
python3 start_coordinator.py --runtime claude --root /path/to/workspace
python3 start_coordinator.py --runtime codex --root /path/to/workspace
python3 start_coordinator.py --runtime cursor --root /path/to/workspace
python3 start_coordinator.py --runtime agy --root /path/to/workspace
```

The launcher copies the rules, scripts, assets, and Claude plugin into `~/.local/share/chief-of-stuff/versions/<version>-<hash>/` and starts the selected CLI with absolute paths to that copy. It refuses a missing runtime binary. `--install-dir <path>` selects another installation location; `--dry-run` installs and prints the exact launch arguments without opening a session. Updating this checkout creates a new release directory; an existing coordinator continues using its pinned copy.

Claude can still use the existing plugin workflow:

```sh
claude plugin marketplace add /path/to/chief-of-stuff
claude plugin install chief-of-stuff@chief-of-stuff
claude --agent chief-of-stuff
```

When `Calendars:` names calendars, configure the `Calendar tool:` MCP server in the selected CLI before launching. The launcher checks that the CLI lists that server and refuses to start if it cannot find it. Each host reports the missing server by name. A calendar integration that disappears during a session is reported to the user before calendar-dependent work continues.

## Workers

After the user approves a dispatch, `scripts/spawn_session.py` starts a separate Ghostty worktree tab with `--runtime claude|agy|codex|cursor`. Claude remains the default. The assignment is written to the worktree before the tab opens; each worker reads it through a fixed bootstrap prompt. Claude uses native session messaging. Other workers register and report through the workspace file mailbox, using their assigned name and worktree. A launcher wrapper loads login zsh configuration for those workers, then records their process under `.chief-of-stuff/sessions/`; `python3 scripts/session_exec.py list --root /path/to/workspace` shows which processes are live.

Cursor starts in [Plan mode](https://cursor.com/docs/cli/overview). Antigravity also starts in plan mode. Codex starts with a read-only sandbox for planning; after plan approval, use the assignment's `session_exec.py resume-codex` command to resume that session ID with write access and update its process registration. Codex documents `/plan` as a [developer command](https://learn.chatgpt.com/docs/developer-commands), but its CLI has no plan launch flag.

Every worker assignment describes a five-minute mailbox check. Antigravity uses [Schedule or `/schedule`](https://antigravity.google/docs/slash-commands/), Cursor uses its in-session [`/loop`](https://cursor.com/docs/agent/overview), and Claude uses its native cron tools. [Codex CLI has no Scheduled interface](https://learn.chatgpt.com/docs/automations), so its workers check at the start of each turn and after each commit. The prompts keep the approval gate for each new task.

Workers can use the pinned `scripts/inbox.py wait --recipient <assigned-name> --timeout 300` for a bounded idle check. It prints unread messages as TOON as soon as they arrive, or `[]` after five minutes, without acknowledging them. A scheduled host check should run `inbox.py list --recipient <assigned-name> --unread` once per tick. These shared commands replace workspace-specific polling scripts under `.chief-of-stuff/`; a wait only runs while the agent's current tool call is active and does not wake a finished conversation.

New mailbox messages are stored as `.toon` files, and the structured results of `send`, `list`, `wait`, `read`, `drain`, and `status` default to TOON. Existing `.json` mailbox messages remain readable and can be acknowledged. Use `--format json` only for older consumers that still require JSON. `send --payload-toon '<TOON object>'` and `send --payload-file message.toon` accept TOON payloads; the existing JSON payload flags remain supported.

Restart coordinators and workers launched from an older pinned release before new workers send TOON mail. An older inbox script only understands JSON and may quarantine a `.toon` message as malformed; new scripts can read both formats during the transition.

The Claude coordinator keeps its 15-minute full check and adds a five-minute mailbox check. Codex, Cursor and Antigravity coordinators have a session-scoped Python watcher that audits the inbox, workers and tasks every five minutes and notifies on findings. Antigravity and Cursor also start their native recurring mailbox prompts when available. Codex reconciles tracker and board state on its next turn because the CLI cannot wake an idle conversation.

## Verification

```sh
python3 -m unittest discover -s evals -p 'test_*.py'
python3 evals/run.py --arm agent --golden --model opus --runs 3
python3 evals/run.py --runtime codex --arm agent --model gpt-6-sol --effort high --golden --case stale-clock
python3 evals/run.py --runtime cursor --arm agent --model <cursor-model> --case stale-clock
python3 evals/run.py --runtime agy --arm agent --model <agy-model> --case stale-clock
```

The eval runner shares fixture setup and graders across CLIs. Claude remains the default backend. Codex accepts the isolated calendar and peer MCP mocks through per-run config. Cursor and Antigravity currently refuse cases that require those mocks, and headless backends refuse wait turns that depend on an idle conversation waking by itself. A refusal is recorded as unmeasured, never as a passing case. See [TEST_INFRA.md](TEST_INFRA.md) for the test setup.
