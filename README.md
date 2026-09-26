# chief-of-stuff

A daily coordinator for Claude Code, Codex CLI, Cursor CLI, and Antigravity CLI. The workspace keeps its existing `CLAUDE.md` `## Coordinator` block. The coordinator routes work, keeps the tracker and board current, and automatically dispatches ready one-shot tasks and asks before new interactive sessions. Every decision it puts to the user links a page with the full context: the question, background, options, a recommendation and the default. With a self-hosted board, `chief-of-stuff decision` renders that page into the pages dir and prints its local URL. Without one, the page is a comment on the issue or pull request the decision is about. A reply asks one decision at a time.

## Install and launch

Install one command from this checkout:

```sh
python3 chief_of_stuff.py install --bin-dir /usr/local/bin
```

This writes a stable launcher in `/usr/local/bin` and a versioned release under `~/.local/share/chief-of-stuff/`. The system directory needs write permission only when first installing or explicitly replacing a different launcher; later installs update the user-owned `current` pointer. Use `--dry-run` to preview the paths without writing. The command routes a fixed set of operations: `start`, `init`, `models`, `worktree`, `worker`, `audit`, `processes`, `board`, `inbox`, `backlog`, `kanban`, `notify`, `health`, and `pages`. Use `chief-of-stuff <command> --help` for each command's options.

For a new workspace, create a minimal `## Coordinator` block and settings file, then launch a coordinator:

```sh
chief-of-stuff init --root /path/to/workspace --user "Your Name" --timezone America/Chicago
chief-of-stuff start --runtime codex --root /path/to/workspace
# also supported: --runtime claude, cursor, or agy
```

The initializer appends to an existing `CLAUDE.md` when it has no Coordinator block, refuses to replace one that already exists, and leaves existing settings intact. It copies an editable provider and model guidance template to `chief-of-stuff-models.md` in the workspace root without replacing an existing copy. For an already configured workspace, run `chief-of-stuff models --root /path/to/workspace` to add only that file. `--dry-run` previews the addition. Optional flags include `--agent implementer:task`, `--launcher tmux`, `--worker-mode one-shot`, `--board-port 8765`, `--github-repo owner/repo`, or `--gitlab-host` with `--gitlab-project`. The board port configures serving. No coordinator or worker may automatically open the board URL or rendered HTML. Render and serve only; the user opens it. Claude launches load a pinned plugin hook that blocks board-opening shell and browser tool calls while allowing render, serve and HTTP checks. Notifications start off. Add calendar and deadline lines when those integrations are available.

`chief-of-stuff start` resolves the selected CLI through the configured interactive login shell (`$SHELL`, or the account login shell) so shell startup paths are honored, and refuses a missing runtime binary. It pins rules and scripts to one release for the life of that coordinator. `--dry-run` prints the exact launch arguments without opening a session. `python3 start_coordinator.py` from the checkout remains available as a source launch.

Claude can still use the existing plugin workflow:

```sh
claude plugin marketplace add /path/to/chief-of-stuff
claude plugin install chief-of-stuff@chief-of-stuff
claude --agent chief-of-stuff
```

The launcher starts even when the selected CLI does not expose the configured `Calendar tool:`. The coordinator reports the missing tool, leaves calendar facts unverified, and continues work that does not depend on the calendar. To enable calendar reads, configure an equivalent calendar tool in that CLI.

If a workspace requires workers to use only the configured `User:` name, add `- Identity: user-name-only` to its `## Coordinator` block. Without that line, assignments make no claim about aliases or names found in workspace files.

The coordinator's PARA filing rule applies only when the workspace `CLAUDE.md` explicitly defines the `Projects/`, `Areas/`, `Resources/`, and `Archives/` homes. Other workspaces do not gain those folders or automatic filing moves.

Notifications are off unless the workspace settings explicitly set `[notify] adapter = "md-notify"`. The default queue path for that adapter is `notifications/NOTIFICATIONS.md` under the workspace; set `queue` to use another path.

For a GitHub `Backlog:` line, the pinned `chief-of-stuff backlog --create <title> --body <text> --commit` files issues through `gh` without a personal plugin. It supports native `--parent` and `--blocked-by` relationships. Writes preview by default; a workspace can still set its own issue content rules. See [backlog setup](docs/backlog.md) for GitHub and GitLab configuration.

An optional `[workflow] architecture_reviewer = "<session-name>"` in the workspace TOML routes filed architecture findings to that session. Without it, no particular person or agent is assumed.
An optional `[workflow] reviewer_session = "<session-name>"` routes pull requests to a named reviewer. Without it, the coordinator asks the user how to review a pull request instead of inventing a reviewer session.
`[workflow] delivery = "pull-request"` and `merge_owner = "user"` preserve the existing delivery path by default. Set `delivery = "branch"` for branch handoff without an automatic PR step, or `merge_owner = "worker"` to let the owning worker merge a reviewed PR after the coordinator confirms required approvals. Branch delivery still needs integration into main and a suite run there before a code task is marked done.

## Workers

Set the workspace worker mode in TOML:

```toml
[workers]
mode = "one-shot" # or "interactive" (the default)
launcher = "ghostty" # used for interactive workers
```

After the user approves an interactive dispatch, `chief-of-stuff worker` starts a separate worktree terminal with `--runtime claude|agy|codex|cursor`. Claude remains the default. Ghostty tabs are the default launcher. Set `[workers] launcher = "tmux"` in the workspace TOML named by the `Settings:` line, or pass `--launcher tmux`, to open a new window in an existing tmux session. A launch refuses a missing tmux executable or tmux server. `--launcher ghostty` overrides the setting for one dispatch. The assignment is written to the worktree before the terminal opens; each worker reads it through a fixed bootstrap prompt. Claude uses native session messaging. Other workers register and report through the workspace file mailbox, using their assigned name and worktree. A launcher wrapper loads the configured interactive login shell for every worker and records non-Claude worker processes under `.chief-of-stuff/sessions/`; `chief-of-stuff processes --root /path/to/workspace` checks all registered workers in one batch and prints TOON. Pass space- or comma-separated PIDs to check a specific batch, including unregistered processes. `gone` means the PID is absent, `pid_reused` means a registered PID now belongs to another process, and `unverified` means the process exists but its identity could not be checked.

Set `[workers] mode = "one-shot"` in `chief-of-stuff.toml` to require one-shot execution for every task worker. Interactive mode remains the default when the setting is absent. In an interactive workspace, pass `--one-shot` when the user requests one-shot for a particular task; this does not change the workspace setting. There is no per-task interactive override for a one-shot workspace. The selected CLI runs once in the foreground, with write access and no worker mailbox or plan gate. Claude uses print mode with automatic permissions and denies anything that would still prompt; Codex uses a writable sandbox with `-a never`; Cursor uses print mode with `--force`; Antigravity uses print mode with tool permissions preapproved. Standard input is closed, so an unavailable permission becomes a worker failure and human-review handoff instead of a stalled prompt. The workspace setting authorizes routine dispatch without per-task approval; a task-specific one-shot request authorizes that launch. The coordinator selects ready tasks, reports progress and blockers, and keeps the tracker and board current. Lane gates and human-review holds still require their configured decisions. The worker reads the scoped Tasks and File ownership rows, writes a one-shot assignment into the worktree, and captures logs and a TOON report under `.chief-of-stuff/`. A worker must write `worker-result.toon` with `status: done`, `status: human_review`, or `status: relaunch`, a `reason`, and `changes`. `relaunch` means the task's premise moved before the worker changed anything, such as its target pull request merging; the launcher sets the row back to ready with no hold, and the coordinator relaunches it in a fresh tree. A relaunch that left changes, or a second relaunch of the same task that day, becomes human review. A missing result, nonzero exit, timeout, or dirty worktree after a claimed completion requires human review. The default limit is 60 minutes; set `--timeout-minutes N` for longer work. When the worker starts, the launcher sets the task's owner to the worker's name and its state to `running HH:MM`, adds the tree and branch to the task's File ownership row, and logs the launch. When the worker returns, the launcher sets the tracker row to `waiting` for the configured user. If review is needed, it adds the configured `human_review_label` while preserving the current numbered stage or GitHub Project Status, and posts an issue comment with the reason and partial changes when an issue exists. For tasks without an issue, the tracker Log carries the reason and changes. The printed report lists any tracker, label, or comment failures for the coordinator to reconcile. `--dry-run` previews the command without writing or launching.

Name each task class's suggested model and fallback order in the same TOML. Each entry is `runtime:model-id@effort`: the runtime is `claude`, `agy`, `codex` or `cursor`, the model ID is used verbatim, and `@high`, `@medium` or `@low` is optional. The first entry is the suggested model; the rest is the order to fall back through when a model is unavailable or out of quota. Class names are free-form, like lane names.

```toml
[models.deep]
rotation = ["claude:opus@high", "codex:<model-id>@high"]
[models.implement]
rotation = ["claude:sonnet@medium", "codex:<model-id>@medium", "claude:haiku"]
[models.review]
rotation = ["codex:<model-id>@high", "claude:opus@high"]
```

`chief-of-stuff models --root . --class implement` prints the suggested entry. `--after <entry>` prints the next one, or exits nonzero when the rotation is exhausted. `--not-family <runtime>` skips that runtime's entries, for an independent review. `chief-of-stuff worker --class implement` launches the suggested entry when no `--runtime` or `--model` is given; explicit flags win. Effort is passed only to Claude workers. A bad runtime, an empty or duplicate rotation, or a bad effort is refused. Without `[models]`, the coordinator follows `chief-of-stuff-models.md`.

Interactive Cursor workers start in [Plan mode](https://cursor.com/docs/cli/overview). Interactive Antigravity workers also start in plan mode. Interactive Codex workers start with a read-only sandbox for planning; after plan approval, use the assignment's `session_exec.py resume-codex` command to resume that session ID with write access and update its process registration. Codex documents `/plan` as a [developer command](https://learn.chatgpt.com/docs/developer-commands), but its CLI has no plan launch flag.

Every interactive worker assignment describes a five-minute mailbox check. Antigravity uses [Schedule or `/schedule`](https://antigravity.google/docs/slash-commands/), Cursor uses its in-session [`/loop`](https://cursor.com/docs/agent/overview), and Claude uses its native cron tools. [Codex CLI has no Scheduled interface](https://learn.chatgpt.com/docs/automations), so its workers check at the start of each turn and after each commit. The prompts keep the approval gate for each new task.

Workers can use the pinned `chief-of-stuff inbox wait --recipient <assigned-name> --timeout 300` for a bounded idle check. It prints unread messages as TOON as soon as they arrive, or `[]` after five minutes, without acknowledging them. A scheduled host check should run `chief-of-stuff inbox list --recipient <assigned-name> --unread` once per tick. These shared commands replace workspace-specific polling scripts under `.chief-of-stuff/`; a wait only runs while the agent's current tool call is active and does not wake a finished conversation.

New mailbox messages are stored as `.toon` files, and the structured results of `send`, `list`, `wait`, `read`, `drain`, and `status` default to TOON. Existing `.json` mailbox messages remain readable and can be acknowledged. Use `--format json` only for older consumers that still require JSON. `send --payload-toon '<TOON object>'` and `send --payload-file message.toon` accept TOON payloads; the existing JSON payload flags remain supported.

Restart coordinators and workers launched from an older pinned release before new workers send TOON mail. An older inbox script only understands JSON and may quarantine a `.toon` message as malformed; new scripts can read both formats during the transition.

The Claude coordinator keeps its 15-minute full check and adds a five-minute mailbox check. Codex, Cursor and Antigravity coordinators have a session-scoped Python watcher that audits the inbox, workers and tasks every five minutes and notifies on findings. Antigravity and Cursor also start their native recurring mailbox prompts when available. Codex reconciles tracker and board state on its next turn because the CLI cannot wake an idle conversation.

The optional [Kanban walk](docs/kanban.md) maps tracker stages to configurable board columns in the workspace TOML. It syncs GitLab issue labels, GitHub issue labels, or a GitHub Project Status field, with `!!` as an independent human hold. Moves are previewed by default, and the audit reports drift without moving cards.

`chief-of-stuff board` writes the board file only. `chief-of-stuff pages --ensure --root /path/to/workspace` serves it and prints a status without opening or printing the URL. Only the user opens the board when they want to view it.

`chief-of-stuff log --root /path/to/workspace "<text>"` appends `- HH:MM <text>` to today's tracker Log, stamped in the workspace zone. Every script-side tracker write, the one-shot launcher's included, holds the same lock on a `.<tracker>.lock` file beside the tracker.

`chief-of-stuff decision --root /path/to/workspace --name <slug>` renders `<pages dir>/decision-<slug>.json` into `decision-<slug>.html` beside the board and prints its `http://127.0.0.1:<port>/` URL. The JSON fields are listed in `scripts/decision_page.py`.

The board and `notify.py sync` also read precise deadlines from tracker `## Decisions` rows: put `deadline: <name> YYYY-MM-DD HH:MM` in the `item` column, or `deadline: <name> HH:MM` for a time on that tracker's date. A later decision for the same name updates its marker, task horizon, and future notification warnings. A decision without an exact time supplies no instant. Run notification sync after a precise deadline decision changes.

## Verification

```sh
python3 -m unittest discover -s evals -p 'test_*.py'
python3 evals/run.py --arm agent --golden --model opus --runs 3
python3 evals/run.py --runtime codex --arm agent --model gpt-6-astra --effort high --golden --case stale-clock
python3 evals/run.py --runtime cursor --arm agent --model <cursor-model> --case stale-clock
python3 evals/run.py --runtime agy --arm agent --model <agy-model> --case stale-clock
```

The eval runner shares fixture setup and graders across CLIs. Claude remains the default backend. Codex accepts the isolated calendar and peer MCP mocks through per-run config. The harness pins its fake `gh` executable by absolute path for backlog writes, even if an agent shell changes `PATH`. Cursor and Antigravity currently refuse cases that require those mocks, and headless backends refuse wait turns that depend on an idle conversation waking by itself. A refusal is recorded as unmeasured, never as a passing case.
