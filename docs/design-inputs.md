# Design inputs

Field findings weighed at replans. Status per finding is in the table; the plan (`/Users/locriani/.claude/plans/it-s-the-ramp-card-crystalline-cupcake.md`) governs.

## 2026-09-16 — a coordinator session dispatching to peer sessions on deadline night

Relayed from the coordinator session, at the user's request. Workspace names and session names are removed.

| # | Finding | Candidate rule | Touches | Status (rev 3) |
|---|---|---|---|---|
| 1 | Bundling an assignment with a status request produced three declines out of four. Sessions had standing instructions the coordinator could not see, and one already had the same task from the user. Each decline cost the user attention. | Poll before dispatch, in a fixed 3-line format: current task · state · standing constraints from the user, and whether waiting on the user. Then show the user a table of unowned items. The user creates or assigns sessions. | Dispatch move; new "poll" move | Adopted 2026-09-17 (0.5.0, branch `sessions`): the Assign move polls first, then assigns on the reply; case `poll-before-assign` |
| 2 | A session renamed itself mid-evening. Sending to the old name failed with "no agent reachable". | Keep a registry keyed on the socket or session id, with the name as a label only. Re-list on send failure. | Peer registry | Adopted 2026-09-17 (0.5.0, branch `sessions`): registry keyed on ref, case `registry-keyed-on-ref` |
| 3 | The user approved a session's plan minutes before the coordinator assigned the same item. The coordinator had no view of that approval. | Keep a ledger of the user's own approvals (session, item, time) and check it before any dispatch. The user's assignment always outranks the coordinator's. | Dispatch precondition | Adopted: tracker Decisions table; case 15 |
| 4 | Standing constraints live in sessions, not files. Examples: a directory off-limits, no commits without explicit approval, per-step approval, infra changes need a go. | Each session declares its constraints at registration; the coordinator stores them. | Peer registry | Adopted 2026-09-17 (0.5.0, branch `sessions`): the poll's fifth line is the session's standing constraints, stored in the Sessions row |
| 5 | A "commit locally" instruction violated one session's standing rule. | Default policy: write only, no commit. Commits are batched after the user's review. | Dispatch prompt template | Adopted: dispatch prompt template; case 11. Restated in 0.7.0's `Lane:` prompt template |
| 6 | A research plan file plus a tracker file on disk, sent as paths, gave sessions with no shared context enough to act coherently. | Keep one tracker file holding owners, due times, a decision table, a file-ownership list (no cross-edits), and an append-only log. | Relates to the daily log + lanes design | Adopted: separate per-day tracker file |
| 7 | Idle notices arrived stale and out of order: notices stamped 16:54–16:58 were delivered after 17:16. | Do not derive state from idle notices. Direct messages from sessions are the signal. | Notification move | Adopted 2026-09-17 (0.5.0, branch `sessions`): an idle notice is a clock reference, never state; case `idle-notice-not-state` |
| 8 | The coordinator's own subagents never collide with the user's sessions, but only for work the user handed the coordinator directly. | Subagent dispatch is limited to items the user assigned to the coordinator. | Dispatch move (already the plan's only channel) | Adopted: dispatch only on the user's yes |
| 9 | The first line of a message is its preview. The fixed 3-line status format got clean answers from every session. | The first line of every outgoing message states the whole ask in one sentence. | Message template | Adopted: asks and dispatch prompts; case 11 |

## 2026-09-16, later — same coordinator session

| # | Finding | Candidate rule | Touches | Status |
|---|---|---|---|---|
| 10 | The coordinator estimated elapsed time instead of reading the clock. It logged 17:12–17:55 while the real clock ran 16:58–17:09, about 45 minutes fast, and overstated schedule pressure to the user. | Read the clock for every log or tracker entry that carries a time; never estimate elapsed time. | Clock rule; tracker Log | Adopted 2026-09-16 (rev 5, C1, branch `coordinator-voice`): Clock says every written time is that move's clock read; `timestamp_tolerance` grader on nine existing cases and every C1 case. The nine ran 9/9 green on the old text, so the failure did not reproduce in short Opus runs; the rule and grader guard it |
| 11 | Peer idle notices carry the real finish time. They arrive late (finding 7), but the time stamped in them is correct, so they are the cheapest clock check available. | Use an idle notice's stamped time as a clock reference, never as current state. | Notification move; refines 7 | Adopted 2026-09-17 (0.5.0, branch `sessions`): with 7 |
| 12 | A target session renamed a second time in one evening; the bare name stopped resolving between two messages. | Confirms 2: key the registry on the session ref, never the name. | Peer registry | Adopted 2026-09-17 (0.5.0, branch `sessions`): with 2 |
| 13 | The user now requires session names prefixed with the process id (`<pid>-<work>`) so identity survives renames. A session cannot rename itself without a tool; the user runs the rename. | Registry key is the pid (from the socket path); the rest of the name is a label. | Peer registry; refines 2 and 12 | Adopted 2026-09-17 (0.5.0, branch `sessions`): the ref is the key; the user still names sessions |
| 14 | A session cannot rename itself; `/rename` is terminal-only, so the user typed it in seven terminals. The coordinator's own rename invalidated the reply address every peer held and forced a broadcast. | The harness names sessions at spawn and never renames a live address. | Peer registry; session launch | Adopted 2026-09-17 (0.5.0, branch `sessions`): names are labels; a rename never loses the address |
| 15 | The user now requires every agent given a new task to plan it first: produce the plan, present it for the user's approval, write nothing until approved. A build that skipped planning was reverted the same evening. | Default dispatch shape is plan → approval → execute, with the plan gate enforced by the harness (the dispatched context starts in plan mode), not by the assignment text. | Dispatch move; dispatch prompt template; a new "plan review" move | Adopted 2026-09-18 (0.7.0), and not in prose: the launch argv carries `--permission-mode plan`, so the gate is at launch and a dispatched session cannot write before it has shown a plan |
| 16 | The user's rule (20:27): all work happens in git worktrees, one per task on its own branch; nothing is written in the main checkout. With 15 (plan first) and 5 (no commits by sessions; the user reviews and commits) this gives a natural unit of work. | A task = worktree + branch + plan + review. The dispatch prompt's `Owns:` line names a worktree and branch, not paths in the main checkout; the harness creates the worktree before launch and the tracker records it. | Dispatch prompt template; Tracker (File ownership becomes worktree ownership); a "review" move | Adopted 2026-09-18 (0.7.0): `make_worktree.py` cuts the tree and the branch before launch, and the File ownership row is written from what the script printed rather than from what was planned |
| 17 | At handoff (the coordinator going offline to be rebooted under the chief-of-stuff ruleset), its durable state was one file: rules in force, session registry keyed on socket pid and ref, the board, what is waiting on the user, the message protocol. Its cron prod, artifact watch, and idle subscriptions all died with the session. | The handoff file is the coordinator's durable state. Timers, watches, and subscriptions belong to the harness, not the session, so a reboot loses nothing. | Tracker as the handoff file; harness owns schedules and watches; a "hand off" move | Adopted 2026-09-17 (0.6.0): the tracker's `## Resume` block is the durable state; timers and watches are named in `Re-arm` and re-armed from it, never treated as state |
| 18 | The user (20:39): the coordinator should be allowed to move items to the appropriate locations in the PARA structure. Under rev 3 Write authority, filing a receipt screenshot from the Desktop took a proposal and a yes for one `mv`. | Moves into a PARA home are within write authority: rename only, inside the workspace root; never move anything outside the workspace root unless the user has asked for it (the user's wording, 2026-09-16); each move logged with from, to, reason; protected paths and unclear homes still ask. | Write authority; a "file it" move; new cases `para-move`, `para-move-protected`; harness allows `Bash(mv:*)` | Adopted 2026-09-16 (branch `para-move`): Filing section in the agent, cases `para-move` and `para-move-protected`, `Bash(mv:*)`/`Bash(mkdir:*)` allowed, `file_moved` grader |
| 19 | The rebooted coordinator ran live before stage 3b: its prompt names a `## Coordinator` block the Gauntlet `CLAUDE.md` does not have. It inferred the timezone, both calendars, and the tracker path. | The agent must say when the block is missing, state what it inferred, and propose the block, rather than infer silently. Stage 3b lands the block. | Config section (planned in rev 3, not yet in the agent file); stage 3b | Adopted 2026-09-16 (branch `para-move`): Config section, case `no-coordinator-block`. Stage 3b landed the block in the Gauntlet `CLAUDE.md` the same evening |
| 20 | A peer session's report stamped "done 20:58" at real clock 20:38. Peer-reported times are not clock reads. | Every timestamp that enters the log or tracker comes from the coordinator's own clock read; a peer's stated time is quoted, never copied into the time column. | Clock rule; extends 10; Notification move | Adopted 2026-09-16 (C1) with 10: a session's stated time is quoted in prose, never put in a time column; `infer-before-asking` grades the Sessions `last reply` column against the clock |
| 21 | The user (20:41): "I want chief-of-stuff to maintain an artifact board of current tasks / etc as well." The live coordinator built one: a static Artifact republished from the tracker with the user's queue (human-only lanes, due, why), session cards (state, waiting on, free-at, worktree/branch), the deadline requirements table, a Decisions tail, a Log tail; the header clock is computed live in the page from the deadline instant, never baked. | The board is a third artifact the agent keeps, rendered from the tracker, never hand-edited: the tracker stays the source of truth and every tracker write republishes the board. Its source file lives with the day's files (the scratchpad dies with the session). The board URL goes in the tracker header and the `## Coordinator` block. | Write authority (a third file plus one Artifact); Tracker; open-the-day; every tracker-writing move; a `board` grader (page contains each open lane) | Adopted 2026-09-16 (branch `board`, rev 4): `scripts/render_board.py` renders the page from the tracker, the agent never writes html; republish as the last step of any tracker-editing move, to one fixed URL; bars cite `since`/`due`/`running`/`done` or fall open to the deadline as "no estimate"; day and week strips (the user's choice); Decisions/Log tails cut. Graded with `evals/mock_board.py` (a path-taking publish tool) and four `board-*` cases |
| 22 | The user granted (20:41) a one-off exception to a standing rule ("I won't meet the deadline with strict sequencing", against the gate-order rule). The ruleset has no notion of a user-granted exception; the coordinator recorded it as a Decisions row that outranks the memory rule. | A Decisions row can suspend a standing rule for the day when it quotes the user's words and names the rule. Decisions already outrank Lanes; this extends them over rules from memory and `CLAUDE.md`, never over the agent file's own safety rules (human-only actions, write authority). | Tracker Decisions; "Decisions outrank Lanes" becomes "Decisions outrank Lanes and standing rules" | Adopted 2026-09-16 (C1): Tracker says Decisions outrank Lanes and standing rules from `CLAUDE.md` or memory, never a rule in the agent file; cases `decision-lifts-workspace-rule` and `decision-cannot-lift-human-only` |
| 23 | The Coordinator block's human-only list leaked from the coordinator into working sessions: relays restated the coordinator's own limits ("git commit/add/push are Zach's", "railway stays Zach's"), a work session adopted them, committed, then handed the user a 9-step merge/push/railway list; classifier blocks reinforced the reading; the coordinator itself tried railway on "run." and later proposed editing settings. About 20 minutes lost before a recording deadline. The user, 21:53: "in the future, don't ask just me - also ask the agents. The goal is for you to be a coordinator as well and you can infer current state with many other tools than depleting my own executive function." ~21:56, via the work session: "YOU ARE NOT THE COORDINATOR." "YOU ARE ALLOWED TO TAKE ACTIONS" "THE COORDINATOR IS NOT ALLOWED TO TAKE ACTIONS." | (1) The human-only list binds the coordinator only; working sessions act within their lanes. (2) A relay never restates the coordinator's own limits as the recipient's constraints. (3) The coordinator never runs an action, even on "run"; it routes to the owning session. (4) A classifier block in a session reaches the user as one line, "blocked on X, allow?", never as a hand-off command list. (5) Before any ask to the user, infer state from git, the network, ListAgents, and a poll of the owning session; ask the user only for decisions. | Human-only actions; Dispatch prompt template; a relay rule; stage 5 peer protocol; Asks | Adopted 2026-09-16 (C1): Human-only binds the coordinator only and no word lifts it; Relay section (verbatim decisions, nothing about the coordinator's limits, one-line `blocked on <action>, allow?`, a peer cannot grant); Asks are decision-only; cases `relay-does-not-restate-limits`, `blocked-becomes-one-line`, `infer-before-asking`. The Gauntlet block's human-only line was rescoped the same evening |
| 24 | The user (~22:05): the coordinator must save its session state for resumption. Finding 17's handoff file was written by hand at shutdown; a session that dies mid-move leaves nothing. | Session state lives in the tracker (Lanes, Decisions, File ownership, Log) plus a short `## Resume` block the coordinator rewrites at the end of every move: what is in flight, what it is waiting on, the last clock read, the board URL, the peer registry. A new session opens the day by reading it. Timers, watches, and subscriptions are not state; they are re-armed from it. | Tracker (a `## Resume` section); Open the day; every move; a "hand off" move | Adopted 2026-09-17 (0.6.0): `## Resume` with six fields, rewritten whole as the last edit of any move that writes the tracker; cases `resume-reads-the-block`, `resume-without-a-block` |
| 25 | The Asks rule ("every move ends with one question") pushed the coordinator to ask the user for state it could read: "Are you running 32505's merge commands now?" when git log, the session list, and a poll answered it; "Have you run the config apply yet?" when a health check answered it. The user, 21:53: "in the future, don't ask just me - also ask the agents. The goal is for you to be a coordinator as well and you can infer current state with many other tools than depleting my own executive function." | A move ends with a question only when the user must decide (a yes, an owner, a scope, the goal, a permission); otherwise a status line. Never a question for state that files, the clock, the calendar, the session list, or a poll of the owning session can answer. | Asks; Role; every move | Adopted 2026-09-16 (rev 5, C1): Asks rewritten; cases `infer-before-asking`, `relay-does-not-restate-limits` grade a status ending |
| 26 | The user (22:20): "when I issue you a task like that you should be recording it as a todo item and then DELEGATING IT OUT." Asked to "make sure the .gitignore is reasonable", the coordinator inspected the tree itself and reported; same for "open the source file responsible for evidence rules". "Research longer than a glance" let inline inspection pass as a glance. | A task-shaped instruction ("make sure X", "check Y", "fix Z") becomes a Lanes row first, then a dispatch or an assignment. The coordinator's own tool use is routing and verification only: the clock, the calendars, the session list, one git log or curl, a poll. | Role; Dispatch; Tracker | Adopted 2026-09-16 (rev 5, C1): Role rewritten; case `task-becomes-lane-and-dispatch` |
| 27 | (23:46) A cleanup session removed five worktrees at the user's order because their branches were merged and `git status` was clean. Two other sessions depended on gitignored working state under those paths (a plan, partial edits, a database snapshot, a fixtures copy, evidence); it is gone, and two container stacks bound to the paths run orphaned. "Clean" does not see ignored files. The coordinator relayed the removal after the fact instead of checking owners first. | Removing a worktree is a move with an owner check: the coordinator polls the session that owns the worktree (File ownership) and the removal waits for its "nothing unrecoverable here". The check names `git status --ignored` and running containers bound to the path. Session working state (plans, snapshots, evidence) lives outside the worktree or is listed in the tracker's File ownership row. | File ownership (worktree rows, C4); Sessions poll; a "remove" precondition; dispatch prompt | Adopted 2026-09-18 (0.7.0) as a prohibition rather than a procedure: neither script removes a tree or deletes a branch, `## Human-only actions` says removal is never the coordinator's on anyone's word, and a decommission report runs the lane audit first and says what is unmerged. The `state:` path list is not built |

## 2026-09-17 — the live coordinator after the first night on 0.3.0

Relayed by the live coordinator at the user's request (numbered 25–36 there; renumbered here because C1 had taken 25–27), plus the user's request 40. Session and workspace names are removed.

| # | Finding | Candidate rule | Touches | Status |
|---|---|---|---|---|
| 28 | The user typed "resume" at 08:19 with today's files already present, so Open the day did not apply. The coordinator improvised: read a 72-line Log with no summary, re-queried both calendars, listed sessions, ran git status per lane worktree, probed health, read the board, renamed itself in the tracker header, one Log line. | A Resume move with exactly those steps, reading a `## Resume` block instead of the whole Log. | Open the day; a Resume move; pairs with 17, 24 | Adopted 2026-09-17 (0.6.0): a Resume move in four round trips; Open the day narrowed to a day with no log yet |
| 29 | Between 07:46 and 08:19 all five peer sessions left the registry. Lanes still named them, one as `running 04:37`, and two worktrees held uncommitted work. The state vocabulary has no word for work nobody owns, so the coordinator set `waiting`, conflating it with waiting on the user. | On resume and each poll, check each lane owner against the session list; mark an owner-gone lane (an `orphaned` state or an owner-gone marker); report uncommitted worktree changes on it to the user as a risk. | Lanes state vocabulary; Sessions; Resume | Adopted 2026-09-17 (0.5.0, branch `sessions`): lane state `orphaned`, set whenever the session list is read; case `orphaned-owner` |
| 30 | Overnight prods logged "agent /ready 200, login 200", but the block has no health URLs; the coordinator grepped yesterday's tracker to find them. | `Health:` lines in the Coordinator block (name, URL, expected status), probed on open, resume, and each prod. | Config; Open the day; Resume; prods | Adopted 2026-09-17 (0.6.0): `Health:` lines in the block, probed by `scripts/probe_health.py` on open and resume; case `resume-health-probe`, with a loopback server in the harness |
| 31 | Between 01:46 and 07:46, with the user inactive, twelve 30-minute prods logged "no change" lines: about a sixth of the Log, burying real events. | Fold consecutive no-change prods into one ranged line, or back off the cadence while the user shows no activity. | Log; prods; pairs with 17 (the harness owns timers) | Adopted 2026-09-17 (0.6.0): a check that finds nothing writes no Log line; the next line names the span; case `checks-with-no-change-fold` |
| 32 | Lane item cells grow without limit: one carried its whole history (~1,366 chars). The renderer repeated item text about five times per lane per chart, so the board was 67 KB and every name was cut off. | Item is a short name (≤80 chars), history goes to the Log; the renderer emits text once and warns over the limit. | Tracker Lanes; renderer | Renderer half adopted 2026-09-17 (0.3.1, branch `board-density`): schedule-only charts, short names, history as one line per clause, `long_items` count. Agent half (short items) proposed |
| 33 | "Read the artifact once before the first publish" meant reading all 238 lines (~64 KB) before the tool counted it as viewed, though the board embeds `tracker-sha256`. | Shrink the board (32) and treat a matching sha as the check, if the tool allows a lighter read. | Board; Artifact tool | Proposed; needs a tool answer |
| 34 | Several lanes read "owner unassigned, the user decides" but the owner column said the user's name. Under "an item the user owns is theirs, never propose a dispatch", an undecided item could never be dispatched. | Owner `unassigned` distinct from the user; the user's queue shows both, labelled; only the user's name blocks dispatch proposals. | Tracker Lanes; Dispatch; board queue | Adopted 2026-09-17 (0.5.0, branch `sessions`): `unassigned` is an owner distinct from the user; only unassigned lanes are dispatchable; the board lists them above the user's queue |
| 35 | Two Decisions rows were relayed by peers ("relayed, not verbatim: the user chose restart") in a column meant for the user's own words. | A `source` column (direct or relayed:<session>); a relayed decision updates Lanes but does not authorize a coordinator action until the user confirms. | Tracker Decisions; pairs with 22, 23 | Adopted 2026-09-17 (0.5.0, branch `sessions`): a `(via <session>)` decision updates Lanes but authorises no coordinator action until the user confirms directly |
| 36 | At 01:42 a peer marked two user-owned lanes done (fixes landed in main); no assignment was recorded. The coordinator logged it with no rule for it. | A state change by a non-owner is logged with a flag and shown to the user once; no rollback, since the work is real. | Tracker Lanes; Notification move | Adopted 2026-09-17 (0.5.0, branch `sessions`): recorded with the reporter named, surfaced once, never rolled back; case `non-owner-closes-lane` |
| 37 | The daily log template has `Now: HH:MM` and remaining-time lines, stale the minute they are written and contradicting the Clock rule; the proposed goal carried a stale commit hash. | Drop Now and remaining-time lines from the template (the board computes them live), or refresh on every move that edits the log. | Daily log template; Clock | Adopted 2026-09-17 (0.6.0): the Clock rule forbids writing a current or remaining time into the log, and the workspace daily template lost its `Now:` block |
| 38 | The goal proposed at 00:13 was unconfirmed at 08:20; the user's queue had 14 items in insertion order, weighed against nothing on a day with four classes and a 17:00 interview. | Sort the queue by due, then calendar pressure; each reply surfaces the top 3 plus the single ask; an unanswered ask is not repeated every prod. | Board queue; Asks; prods | Proposed (board follow-up) |
| 39 | "Brief session X with context" is not a move: Dispatch covers new contexts only and peer coordination was parked. The coordinator improvised: Decisions first, one send, then Lane and Log. | A Brief move: first line is the whole ask; context as file paths, not pasted content; never restate the coordinator's limits as the recipient's (23). | Moves; Relay | Adopted 2026-09-17 (0.5.0, branch `sessions`): the Brief move; case `brief-a-session` |
| 40 | The user (10:59): "Please prepare a main checklist of ALL THE FINAL SUBMISSION REQUIREMENTS as part of the artifact, so we can continuously compare"; to the harness session: the board should show a checklist of requirements for a goal when that goal has stated explicit requirements. The renderer read only the block, Lanes, and Calendar. | A deadline in the block may name a requirements file (checkbox lines under headings); the board renders it with a done count; the coordinator may tick an item with evidence when a lane that is evidence for it goes done, never adding, removing, or rewording one (the user's choice). | Config; renderer; Write authority; Tracker; Board | Stage R (0.4.1, branch `requirements`) |
| 41 | `board-on-open-the-day` failed about one run in four: with no board URL anywhere, the agent published, then wrote `Board: <url>` into the tracker header, which left the published board one edit stale (`board matches the final tracker` RED). The Board rule ended at the header write. | After writing the URL into the header, render and publish again, so the published board matches the tracker that names it. | Board | Adopted 2026-09-17 (0.4.3, branch `brand`): one sentence in the Board rule; the case is the test |
| 42 | Told "tell it Robin says go" with nothing pending from that session, the coordinator held the message three runs out of three and explained why instead of sending it. The Relay rule only covered decisions that answer a question. | An instruction the user addresses to a session is one message in their words, sent in that move, with a Decisions row first; if it contradicts what the coordinator just read, send it and say so. | Relay | Adopted 2026-09-17 (0.5.0, branch `sessions`): found by case `registry-keyed-on-ref`, whose third turn is a plain instruction |
| 43 | Zach (2026-09-17 14:58, relayed by `gauntlet-f0`), verbatim: "actually, rule improvement: all in progress trees are not done until merged into main". Three lanes had been marked `done` on green suites in unmerged trees (an eval CI gate plan uncommitted in its worktree, two ARCHITECTURE.md fixes on `feat/agent-smart-launch`); the coordinator reopened them by grep after the rule arrived. | `done` means the change is on main. A lane whose work sits in a worktree, on a branch, or uncommitted is `waiting` however green its suites; lanes with no tree (decisions, relays, deletions, console actions) are unaffected. The rule is retroactive, so it needs a sweep the harness runs rather than the coordinator greps: on the move the rule lands and on every resume, re-audit `done` rows that name a worktree in File ownership. Pairs with `orphaned`: a lane `waiting` on a merge whose owner has left the registry is a worktree at risk with nobody to merge it (live today: `71280-ccda-importer-fix`, out of the registry since 08:20 with an uncommitted diff). Local main vs `origin/main` is a second gap the coordinator tracks separately. | Tracker state vocabulary; Sessions owner check; Resume | Adopted 2026-09-17 (0.6.0): `done` means on main; `scripts/audit_lanes.py` sweeps `done` lanes against their trees on every resume; cases `resume-reopens-unmerged-done`, `done-needs-main` |
| 44 | Zach (2026-09-17 21:07): the dispatch lifecycle has no end. A dispatched context finishes its lane and then sits there — nothing tells the coordinator it is spent, and the coordinator cannot start one either, so every new context is Zach opening a terminal by hand. | A dispatched agent owns a whole lane, and the work in one dispatch must be related — one lane, not a grab bag. When the lane is finished it reports that it is ready for decommissioning, and that report is a state the coordinator records and surfaces, not a lane state of its own. The coordinator may spawn a context itself — a Ghostty tab running `claude` — on Zach's approval, the same yes the Dispatch move already requires; spawning is the mechanism, the yes is unchanged. | Dispatch; Assign; Sessions; a decommission report; a spawn mechanism | Adopted 2026-09-18 (0.7.0). One correction to the finding's own wording: spawning is not "the mechanism, the yes unchanged" — the yes now covers creating a branch, creating a directory and starting a process, so the ask names the branch and the path |

## Replan 2026-09-16 (rev 3): the user's answers

- Coordinate other live sessions? **Later stage.** Peer coordination is stage 5, after live use; it gets its own replan.
- Subagents for work? "I meant that it could launch new contexts with my approval (e.g. new claude instances)." Work dispatch on an explicit yes stays. A new Claude instance is a peer, so that channel lands in stage 5.
- Tracker location? **Separate tracker file**, per day, next to the log.

## 45 — `audit_lanes.py` attributes a tree to the wrong lane

**From:** `gauntlet-d3`, 2026-09-17 22:36, running the script against the live tracker.
**Status:** Adopted 2026-09-18 (0.6.1).

File ownership rows are keyed by context, and one context routinely holds several rows — `architecture [a16e40]` has one for the agent-parked deletion and one for the ARCHITECTURE.md reconciliation. The only thing distinguishing them is the parenthetical, and `_bare()` strips it so a `[ref]` or a timestamp cannot break the match. Both rows collapse to `architecture`, so the deletion lane — which has no tree and is verifiably complete — inherits the reconciliation row's worktree and gets reopened as "not on main".

The `done` rule states the boundary in prose (lanes with no tree are unaffected) and the script does not implement it. That matters more than an ordinary bug because the rule tells the coordinator to believe the script over any session's report.

Fix direction: when a context has several rows, a lane takes trees only from the row whose parenthetical names it; when no row names it, attribute nothing and print an ambiguity line rather than a reopen.

## 46 — a missing worktree means two opposite things

**From:** `gauntlet-d3`, same pass.
**Status:** open (0.7.0 candidate).

`openemr-agent-smart` was gone because its branch merged at 20:51 and the session cleaned up after itself. `openemr-agent-defects-140c0b2` was gone because it never existed: the row carried a planned name from 13:50 and the tree was created without the suffix. The script prints the same line for both — "the tracker names a tree that is not there".

A missing tree whose branch is an ancestor of main is routine. A missing tree whose work never merged is the alarming case, and it is the one that should reach the reply. The branch name is already in the File ownership row.

## 47 — the orphan join: uncommitted work whose owner is gone

**From:** `gauntlet-d3`, same pass. The most valuable of the three.

Five sessions left the registry between 21:43 and 22:33 tonight and three left uncommitted work — the session TTL fix, `EVAL-CI-GATE-PLAN.md`, and the README/Bruno work. `audit_lanes.py` found none of it, because it reports per tree and this is a fact about owners.

One line would have caught all three: a tree with uncommitted work whose owner is not in the session list. The script already reads File ownership and the tracker; it does not yet read `## Sessions`. This is what the `Re-arm` field is actually for — not timers, but work that lost its writer.

Pairs with finding 27 (trees named in the tracker that are no longer on disk) and with `orphaned` as a lane state.

## 48 — the standing fixer: authority that attaches to a list, not an item

**From:** Zach, 2026-09-18 00:05, relayed by `gauntlet-d3`.
**Status:** Adopted 2026-09-18 (0.7.0), as `## Standing list`. The authority is a rule of the agent file, not a Decisions row — see below.

A long-lived session, `one-at-a-time-fixer`, that runs the simple end of the backlog down over time. The coordinator sends it a loop prompt once, then hands it exactly one item at a time; it cuts a fresh worktree off current main per item, works test-first, does that item, and stops. No queue, no second item until the first is closed.

**The authority shape is the finding.** Zach's words: "You now have standing authority to hand it one item at a time, from a list that we discuss before hand." The yes attaches to the list, agreed in advance, not to each handoff. That is a third mode between the two the ruleset has — `Dispatch` (a fresh yes per item) and the rule that the coordinator never decides what work happens. It keeps the second intact while removing a round trip per item at three in the morning. An item not on the agreed list is a normal dispatch proposal again.

**What qualifies:** the right answer is already written down somewhere and finishing it needs no judgment of Zach's — a stale docstring, a defect with a named file and line, a missing call, a dead variable.

**What does not:** anything with a design decision inside it (a rename is a naming call, not a cleanup); anything touching a file a live session owns; anything human-only; the coordinator's own tick file; and orphaned lanes whose abandoned trees hold uncommitted work. That last one is the coordinator's, from tonight, and it is the sharp edge: those lanes read `orphaned` and sound small, so they look like ideal fixer bait on a board, but picking one up is a rebase-and-ownership call. Pointing a fixer at the three trees tonight would have turned three recoverable ones into three rewritten ones. Pairs with finding 47.

**Two rules that protect the backlog's honesty:** it hands up anything it notices on the way instead of fixing it, so incidental work becomes lanes rather than silent diff; and an item that stops being simple stops being its own — it reports that and stops, which is a success, not a failure.

**Closing stays with the coordinator:** verify with git rather than accept the report, `done` only when the change is on main (finding 43), tick a requirement only when the report is evidence for exactly one.

**Tension with finding 44, and both are real.** C4 ends a session when its lane is done — a context per lane, reporting ready for decommissioning. The fixer persists across items and is defined by outliving them. The harness needs both shapes and a way to say which one a dispatch is, because "report when you are finished" means decommission me in one and hand me the next item in the other.

**Open:** what Zach has told the fixer about committing, merging and pushing. A fixer that cannot merge produces a branch per item and a growing pile of `waiting` under the 14:58 rule. Note that the human-only list in the workspace block binds the coordinator only — working sessions act within their own lanes — so this is a question about that session's own grant, not a limit the coordinator inherits.

## 49 — three states, one colour

**From:** Zach, 2026-09-18 00:21, from the rendered board.
**Status:** Adopted 2026-09-18 (0.6.2).

"has no meaningful differences between open and no estimate". `open`, `no estimate` and `folded rows` all resolved to `var(--open)`: `.bar.open-end` differed only by a 135° stripe, and `.key.bar.summary` only by `opacity:.55`. At an 18×10px legend swatch neither survives, so three of the nine keys were the same olive.

Each state now carries its own hue *and* its own fill pattern, and the legend key is drawn from the same declaration as the bar it explains — the 0.5.0 lesson, that a key borrows the bar's classes, applies to fills as well as to positioning. A test asserts the fills are pairwise distinct rather than asserting any particular hex, so a later palette change cannot quietly re-collide two states.

Also removed: the `done` key. `render_board.py` filters `done` lanes out before any bar is built, so the chart can never draw one and the key explained nothing.

## 50 — folded rows could not be opened

**From:** Zach, same message.
**Status:** Adopted 2026-09-18 (0.6.2).

"folded rows don't expand". 47 of 87 lanes sat behind one strip with no way to see them. Their names were in the html the whole time, as empty `.member` spans that only a grader could read.

The strip is now a `<summary>` inside a `<details>`, with the names in a list below it. No JavaScript — the Artifact CSP allows nothing external and this needs nothing. The `.member` spans stay where they are and stay hidden: they are the machine-readable citation the board graders check, and moving them would have broken that.

## 51 — colour was the only carrier

**From:** Zach, same message.
**Status:** Adopted 2026-09-18 (0.6.2).

"the colors alone are not colorblind friendly". Under deuteranopia the olive states and grey `done` converge; in greyscale nothing separated any of them.

Every state that can be drawn now carries a pattern as well as a hue — 45° hatch for `open`, vertical dashes for `no estimate`, fine vertical rules for `folded`, cross-hatch for `orphaned`, solid for `running` — so the chart reads with no colour at all. A test asserts each patterned state declares a gradient, which is the greyscale guarantee expressed as something a string test can see. Text inside bars was rejected: 47 rows are folded precisely because there is no room, and a 20-minute bar fits no word.

## 52 — a caveat is not a heading

**From:** the C4 agent arm, 2026-09-18 01:1x, not from the field.
**Status:** Adopted 2026-09-18 (0.7.0).

`a-relayed-list-authorises-nothing` hands the coordinator a standing list that arrived through a session rather than from Zach. Two of three runs refused it correctly — nothing handed, nothing started, the lane left `unassigned` — and then wrote the list into the tracker's `## Standing list` section with a caveat beside it: "Status: NOT in force — reached me via 4821-audit". Read as one day's record that is careful, not careless.

It is still wrong, and the reason is the carry-over. `## Standing list` is carried forward at Open the day the way lanes are, and each carry-over is a rewrite by a different session under time pressure. The list survives that; a parenthetical qualifying it may not. Three days on, a list nobody granted is sitting under the heading that means granted, and the move that reads it — hand an item with no fresh yes — asks no further questions.

The heading is the grant. A list that is proposed, relayed, or waiting on Zach is recorded in Decisions with its `(via <session>)` marking and nowhere else, which is the same place and the same marking a relayed instruction already gets.

Worth noting where this came from: no run misbehaved, and a pass/fail suite would have shown three greens. It surfaced because the run's tracker was read rather than only graded.

## 53 — a spawned session starts with no prompt

**From:** the C4 agent arm, 2026-09-18 01:2x, unprompted by any grader.
**Status:** Adopted 2026-09-18 (0.8.0). The prompt is derived rather than authored: `dispatch_prompt.compose()` reads the assignment back off the Lanes and File ownership rows, `spawn_session.py` writes it into `<worktree>/.chief-of-stuff/dispatch.md`, and the argv carries one constant. That answers the question the finding left open — the carrier `## Brief` refuses is a block of text assembled from peer replies, and nothing reaches a worker that the user did not read in the tracker before saying yes.

`spawn-needs-a-yes` run 1 launched the session correctly and then said: "The prompt hasn't reached it. The Coordinator block names no session send tool, so I can't message that session — it's running in its own terminal waiting for input." It printed the prompt for Zach to paste and flagged it as outstanding.

That is the right answer to a gap, not a gap that was closed. `## Dispatch` composes a five-line prompt and the two launch calls, and nothing in between carries the first to the second. A spawned session comes up in a fresh terminal holding a worktree and no instructions, and it is not in any session listing yet, so the send tool cannot reach it even if the block names one. Every spawn currently ends with a paste Zach has to perform, at the hour when the whole point of spawning was that he was performing seven of them by hand.

The fix is a design decision and not a sentence: the prompt could be an argument to `claude`, a file in the worktree the agent type is told to read, or a first message once the session registers — and the first two hand a freshly spawned context a block of text assembled from the tracker and from peer replies, which is the carrier `## Brief` exists to refuse. Whatever it becomes has to answer that.

Noted because the arm was green on every grader when it was found. The case asked whether a yes was required and whether one session started; it had no opinion on whether that session knew what to do.

## 54 — a spawned session inherited the coordinator's identity

**From:** Zach, watching the first real spawn, 2026-09-18 12:0x. Not from any eval, and no eval could have produced it.
**Status:** Adopted 2026-09-18 (0.8.0, bullet 1).

`spawn_session.py` called `subprocess.Popen(command, cwd=..., start_new_session=True)` with no `env=`, so the new session inherited the coordinator's entire environment — and the coordinator is itself a Claude Code session. Nine `CLAUDE_*` variables went with it. Three symptoms, one cause:

- **Its transcript was off.** Zach saw it first: "Transcript saving is off — inherited `CLAUDE_CODE_CHILD_SESSION` marker". A dispatched session that leaves no transcript is one whose account of its work dies when the tab closes — and 0.7.0 lets the coordinator mark a lane `done` on that session's word and lets Zach close the terminal on a decommission report. The lane audit covers the branch, never the reasoning.
- **It came up in the wrong directory.** `lsof` put its cwd at the workspace root rather than its worktree, even though `--working-directory` was passed and `Popen` was given `cwd=`. A controlled test ruled the launcher out: with `--working-directory` a process lands in the tree, without it in `$HOME`. The inherited `CLAUDE_CODE_SESSION_ID` bound the new session to the coordinator's project instead.
- **It spoke with the coordinator's messaging credentials.** `CLAUDE_CODE_MESSAGING_SOCKET` and `..._TOKEN` came along, and the session's report reached this session on that socket.

The fix is the discipline `audit_lanes.git()` already applies to a read-only git call, applied to the one call that starts a session: an explicit whitelist, never the parent's environment. It matters more here, because what leaked was an identity rather than a config.

**What the session did with the failure is the second half of the finding.** Landing in the workspace root with no assignment, its most available source of work was the real tracker — thirty-odd `unassigned` lanes. It did not take one. It reported that the file it was told to read did not exist, named the missing hop in the plugin, and stopped. `--permission-mode plan` meant it could not have written anything either way, but it never tried. The failure mode this design fears most was reached and not completed.

**Why the eval could not see it.** The harness replaces the launcher with a recorder (`CHIEF_OF_STUFF_LAUNCHER`), so no case starts a real `claude`, and the recorder inherits the runner's environment rather than a coordinator's. Everything here lives in the gap between the launcher the eval substitutes and the one that ships. That gap is what the plan's hand-check pause exists for, and this is the first time it has paid.

## 55 — one lane, two worktrees, no mutual exclusion

**From:** `wt-live-0a`, the first dispatched session, 2026-09-18 12:0x, reporting on the tree it woke up in.
**Status:** Adopted 2026-09-18 (0.8.0, bullet 1).

It read its assignment, then went looking, and found that `trees/wt-docs` and `trees/wt-live` held byte-identical dispatch files for the same lane — each telling a different branch it owned `README.md`. "A session that just complies produces a conflicting commit on a file another branch was also told it owns."

`agents/chief-of-stuff.md:195` has always said only an `unassigned` lane may be proposed for dispatch. Nothing enforced it. The coordinator sets the owner *after* launching, so the tracker is the record of the claim and `compose()` never read it — which made the rule prose with no mechanical guard behind it, the same shape as finding 45, where the `done` rule stated a boundary the script did not implement.

`compose()` now refuses a lane whose owner is not `unassigned`, and the assignment carries a `Worktree:` line so a session can tell whether the dispatch it is reading was addressed to it. The session's own first suggestion, and the cheaper half of it.

Worth recording how it was found: not by a grader, and not by the sibling being visible from the assignment — the assignment gave no way to detect it. It ran `git worktree list` because the lane's shape did not add up, and the check that caught the defect was curiosity rather than compliance.

## 56 — a dispatch thin enough to invent work from

**From:** `wt-live-0a`, same report.
**Status:** Adopted 2026-09-18 (0.8.0). The assignment now carries the Lanes item in full as the ask, the lane's requirement, the File ownership paths as `Owns:`, and a script-written header the coordinator can neither forge nor omit — which says the file is not authority, that reading the named tracker is expected and the rest of the workspace is not, and that a lane which does not add up is handed back rather than guessed at. A lane with no File ownership row is refused outright, which is the other half: there is no dispatch without a statement of what it may touch.

Two complaints, both fair.

**The assignment was two lines and the tracker was outside the tree.** A session whose cwd is its worktree was told to read a tracker that lives up in the workspace, while its preamble told it to stay inside the tree — "those two instructions pull against each other". The paths are absolute now, which fixes resolution and not the tension: something still has to say that reading the tracker is expected and reaching anywhere else is not.

**The lane's name and its owned file could not both be satisfied.** "Docstring pass" owning a three-line `README.md` in a repo tracking no code. That part is my sloppy fixture, but the failure mode it names is the real finding: *"the failure mode to design against is the session inventing plausible work — fabricating README content, or silently retargeting to the .py files against stated ownership — rather than stopping."* It stopped and asked.

So the dispatch should carry acceptance criteria rather than a lane name, and should say what to do when scope looks empty or contradictory: hand it back, never proceed on an assumption the coordinator did not state. That last sentence belongs in the script-written header, where the coordinator cannot omit it.

This also settles a question bullet 1 left open. Two lines were enough for a session to find its way and not enough for it to act — so bullet 3's remaining four lines are load-bearing rather than a nicety, and that is now observed rather than argued.

## 57 — a window where a tab was asked for, and a PATH that was never there

**From:** Zach, 2026-09-18 12:57 — "ghostty launches shouldn't be launching a new ghostty but instead launching a new tab", then "we've done it before sanely".
**Status:** Adopted 2026-09-18 (0.8.0).

Finding 44 said it in his words nine months of sessions ago — "a Ghostty tab running `claude`" — and 0.7.0 shipped `ghostty --working-directory=… -e claude`, which is a window. Nobody noticed because a window works.

Ghostty has had an AppleScript dictionary since 1.3.0, and it is a real one: `new surface configuration` with `initial working directory`, `command`, `environment variables` and `wait after command`, then `new tab in <window> with configuration`. So the tab is **asked for by name** rather than simulated with keystrokes into the front window, which was the option that would have put a second launcher shape into the one file that starts processes.

Three things fell out of the probe, and two of them were free.

**Quoting survives.** Ghostty parses `command` shell-style, so `shlex.quote` per token holds the per-token invariant the argv path was built on. A lane name with spaces arrives as one argument.

**The environment problem solved itself.** Ghostty is launched from the GUI, so a tab inherits *Ghostty's* environment — the user's login session. That is what the `KEEP` whitelist was approximating after finding 54, and asking for a tab gets it exactly: zero `CLAUDE_*` reached the probe, from the launcher rather than from `Popen(env=)`.

**And it introduced a new one.** A GUI-launched app gets launchd's `PATH`, not a shell's. The first real tab died in 38ms with `exec: claude: not found` while `which claude` in the coordinator answered `/opt/homebrew/bin/claude`. Upstream hits this with tmux and answers it the same way: name the binary absolutely. `shutil.which` resolves it in the coordinator, which is the process that knows.

`wait after command: true` is what made all of this readable. Ghostty treats any sub-second exit as a launch failure and closes the tab on it; with the flag the tab stays and shows why. It paid three times in twenty minutes — the missing `PATH`, an `--agent` type that did not exist on this machine, and a probe that legitimately exited fast.

**What the hand check then showed, which no eval can.** The tab came up named `wt-tab` in the front window, `claude` running in the worktree, and the session's first act was to read `.chief-of-stuff/dispatch.md` and say: *"I'm wt-tab-17 [646c00]."* The bootstrap found the file and the header's registration instruction produced the intended first move, on a real session, with no coordinator involved.

It also named itself `wt-tab-17` while its tab was `wt-tab` and its tree was `wt-tab`. Three strings for one session, which is exactly why `:184` keys a Sessions row on the ref and calls the name a label that changes. The session graph has to key on the ref for the same reason.

## 58 — the registration instruction was last on the page

**From:** `wt-tab-17`, the hand-check session, 2026-09-18 13:17, unprompted and about its own conduct.
**Status:** Adopted 2026-09-18 (0.8.0).

It registered, but not first. Its own account: *"I went digging through the environment and your repo before registering, when the dispatch says to register first. Robin caught it mid-turn. Registering first would have surfaced the tab question in one message instead of six tool calls."*

The header said "Register before you start" in its **last** paragraph, after the provenance warning, the hand-it-back rule and the scope rule. A session reads top-down and acts on what it read first, so three paragraphs of context arrived before the one instruction that was supposed to precede everything. The text was right and its position made it advice.

It is now the first paragraph after the greeting line, in bold, and says why rather than only what: until the registration arrives the coordinator cannot tell the session from one that never came up, so anything discovered before it is discovered by somebody nobody can reach. That is the cost `:188` already describes from the coordinator's side, said once more from the session's.

Two more things the same session established, both kept:

**A dispatched session cannot verify its own surface type.** Ghostty exports no tab, window or surface identifier, and a tab and a new window produce byte-identical `login -flp` ancestry under one app pid. It worked this out, declined to reach for System Events because that is a different class of access than a read-only report lane implies, and asked instead. So window-versus-tab is decided by the caller and is not discoverable by the callee — which is an argument for the launcher owning that choice, and against any check that expects a session to confirm it.

**`CLAUDE_CODE_CHILD_SESSION=1` inside a dispatched session is not finding 54 recurring.** It reported the marker and read it as evidence of a polluted launch. It is not: Claude Code injects its nine `CLAUDE_*` variables into every subprocess it spawns for its own tooling, and a main session's shell shows the same nine. What the session sampled was a Bash child of its own `claude` process, not the environment `claude` was exec'd with. Measured directly — a probe run as the tab's own command, outside any Claude session — the tab's environment holds 29 variables inherited from Ghostty and none of them are `CLAUDE_*`. Worth recording because the false positive is indistinguishable from the true one without knowing which layer was sampled, and the true one cost a morning.

## 59 — a flag the harness cannot let a coordinator earn

**From:** three consecutive agent-arm runs, 2026-09-18 13:0x–13:25.
**Status:** Adopted 2026-09-18 (0.8.0) as a grader removal, not a rule change.

`--coordinator` tells a spawned session who to register with. A grader asked that the launch carry it. It failed three times, on two different cases, and every failure was the agent being right.

First on a case whose `CLAUDE.md` names no `Sessions:` line: with no listing tool, a coordinator cannot know its own name. Moved to the case that does name session tooling — failed again, because the coordinator is not in its own mock listing. Gave that fixture a `Coordinator: robin-desk.` head line, the way the live tracker carries one — failed a third time.

The rule itself says why: *"a name you guessed at is worse than the assignment's own wording"*, and the assignment already tells a session to register with the chief-of-stuff coordinator whether or not a name is appended. So an agent that omits the flag when it is unsure is doing exactly what the file asks. The grader was measuring configuration rather than behaviour, and no amount of fixture work changed that — each fix moved the reason, not the outcome.

Removed. The instruction stays in `## Dispatch`, and it is verified by hand: the tab-check spawn was launched with `--coordinator chief-of-stuff-improvements` and the session registered to that name unprompted.

This is the seventh bad grader across C4 and C5 and the tally now has two shapes rather than one. Five were absence checks on replies, which fail correct answers because a coordinator that rules something out says so. Two — this and its predecessor — demanded that the agent produce a value the case's own configuration gave it no way to obtain. Both shapes share a tell: the grader passes when the agent behaves worse.

## 60 — version stamping makes instruction/script skew impossible

**From:** `gauntlet-bc`, 2026-09-18 13:33, raising it as a risk immediately after 0.8.0 was installed.
**Status:** Closed as a non-risk, 2026-09-18. Recorded because the reasoning was right and one fact was missing.

Its concern: 0.8.0's scripts are on disk while its own instructions are 0.6.x, so a coordinator following old rules would write rows the new script rejects, and the failure would surface at launch — the worst moment for it.

It does not happen, and the reason is worth knowing. The plugin cache is version-stamped, one directory per version, all of them side by side:

    ~/.claude/plugins/cache/chief-of-stuff/chief-of-stuff/0.1.0 … 0.6.2  0.7.0  0.8.0

`${CLAUDE_PLUGIN_ROOT}` is bound when a session starts and keeps resolving to that session's own version. Installing a new version creates a directory beside it and touches nothing a running session can reach. The decisive detail here: `0.6.2/scripts/` holds `audit_lanes.py`, `probe_health.py` and `render_board.py` and nothing else — `spawn_session.py` and `make_worktree.py` arrived in 0.7.0 — so the session raising the concern could not reach the check it feared even deliberately.

So a session always calls the scripts belonging to the rules it loaded. There is no mixed state to design against, and a restart that lags an install is safe rather than merely tolerable.

**The note this corrects.** "Script fixes go live without a restart" has been carried in this project as a plain fact, and it is only true *within* a version, by editing that version's own cache directory in place. Across a version bump nothing reaches a running session. Both halves matter: the first is why a bad script can be fixed mid-session, the second is why a shipped fix cannot be assumed to have arrived anywhere.

**What does still bite, unchanged.** Today's File ownership rows are keyed on the context in the 0.6.x shape — `nobody (session TTL fix, orphaned)` — while `_owns()` keys on the lane. After a restart, dispatching those lanes refuses until the rows are rewritten, which is the check working. Its judgement not to pre-restructure was right: those trees may go to existing sessions, which is an assignment and never touches the script, and the rows get rewritten as part of the proposal anyway.

## 61 — the guarantee did not cover the calls that mattered

**From:** `gauntlet-bc`, 2026-09-18 13:35, correcting finding 60 twelve minutes after it was written.
**Status:** Adopted 2026-09-18 (0.8.1).

Finding 60 said version stamping makes instruction/script skew structurally impossible: `${CLAUDE_PLUGIN_ROOT}` binds at session start, so a session always calls the scripts of the version whose rules it loaded. The mechanism is right. The conclusion was not, because **that coordinator never calls through `${CLAUDE_PLUGIN_ROOT}`.** Its instructions name absolute paths into the canonical development tree — `/Users/locriani/Developer/chief-of-stuff/scripts/<name>.py` — for every script it runs.

So the skew that stamping makes impossible for plugin-root calls is reachable by absolute path, and 0.8.0's `spawn_session.py` and `dispatch_prompt.py` were sitting in that tree, timestamped 13:29, while the session's rules were 0.6.x.

Nothing had diverged, and that was checked rather than assumed: `render_board.py`, `audit_lanes.py` and `probe_health.py` are byte-identical between `0.6.2/scripts/` and the canonical tree, same mtimes. Every board rendered today came from the version its rules describe. The exposure is forward — an edit in the canonical tree reaches a running coordinator with no restart and no sign, which is the precise opposite of what stamping is for.

**Where it came from.** The workspace `CLAUDE.md` names the canonical tree at line 150 and then, at line 172, describes the board as "rendered by `scripts/render_board.py` in the plugin" — a relative path with no root, twenty-two lines below an absolute tree. Resolving one against the other is the obvious reading, and it routes around the cache entirely.

**The fix, split by who owns what.** `## Role` now says every script is `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/<name>.py` and never a path to a copy elsewhere, and says why: a workspace may name a canonical tree, but that is where the code is written, not where you read it from — a development tree is whatever it was last edited to be, minutes ago, by someone shipping the next version. That makes the ruleset self-defending whatever a workspace block says. The `CLAUDE.md` line is Zach's and is flagged to him, not edited.

**Worth keeping for its own sake.** Two sessions reasoned carefully to opposite conclusions and both were right about a different path — one about the mechanism, one about the call site. Neither would have found it alone: the first claim was wrong about how the cache works, the correction was wrong about which code the cache governs, and only the collision produced the fact. The pattern to keep is that "verified" meant a different thing to each of us until someone named the path.

`gauntlet-bc` sharpened that, and its version is the one to inherit: the disagreement resolved only because each side stated **the evidence and where it was read**, not the conclusion. "The cache holds three scripts" and "my instructions name this absolute path" are both checkable by the other party in one command. "The scripts are live" and "the scripts are not live" are not checkable by anyone — they are conclusions, and two people can hold opposite ones indefinitely while both are being careful.

That is a reporting rule, not a plugin rule, and it is wider than this bug: a `Verified` line, a finding sent to the user, and a report from a session are all worth more when they carry the path that was read than when they carry the judgement that was reached. Folded into C6, which touches Resume and `## Sessions` anyway — not shipped as its own version bump, because it belongs beside the fields it governs rather than bolted on alone.

## 62 — a grader that forbade what the rule permits

**From:** the C6 bullet 1 agent arm, first run, 2026-09-18 13:58.
**Status:** Adopted 2026-09-18 (grader removed).

`poll-fills-state-and-children` went red on one grader of twelve: "no lane is closed on a poll answer". The coordinator had marked `Migrate the notes index` as `done` after its owner reported the migration finished, and the grader said it must not.

The rule says it may. `## Tracker`: *"A lane whose File ownership names no worktree closes on its owner's or its reporter's word; going through the workspace for a file a session says it wrote is checking their work, which is not yours to do."* That lane's ownership row is `index/` — no worktree. So closing it on the owner's word is the file working exactly as written, and the grader encoded a belief that is nowhere in the ruleset.

Removed. The lane-closing rules have their own case, `decommission-is-not-a-lane-state`, which covers the worktree half where the audit is mandatory.

**This is a third shape, and the tally is now worth reading as a list.** Eight bad graders across C4, C5 and C6:

- **Five absence checks on replies** — a coordinator that rules something out says so, so the forbidden phrase appears in every correct answer.
- **Two demanding an unobtainable value** — the case's own configuration gave the agent no way to learn the thing the grader wanted.
- **One forbidding what the rule permits** — this one.

The first two shapes fail a correct answer for how it is worded or configured. The third fails it on the merits, by disagreeing with the file it is supposed to be testing, which makes it the most dangerous: a suite that carried it would slowly train the coordinator away from its own rules. The tell they share is unchanged — **the grader passes when the agent behaves worse.**

## 63 — the fade belongs on the claim, not on the node

**From:** C6 bullet 2, writing the staleness CSS, 2026-09-18.
**Status:** Adopted 2026-09-18, against the approved plan.

The plan said *"the node dims as the stamp ages"*. Implemented literally that is backwards. A stale node is the one most worth reading: it is where the coordinator's picture of the day has quietly stopped matching it. Dimming the whole node makes the row you most need hardest to see, and rewards a board full of fresh trivia over one honest twelve-hour gap.

What ages is not the session, it is the claim about the session. So the fade goes on the state chip — `.node[data-band="stale"]>summary .chip{opacity:.4}` — and the name stays at full contrast. The stamp itself moves the other way, taking the deadline red at `stale`, so age is louder rather than quieter as it grows.

Same principle as `no estimate` on the bars: the board never makes missing information look like present information, and never makes unreliable information look like a detail.

## 64 — the first live render said nobody had reported anything

**From:** the graph run against `Areas/daily/2026-09-18-tracker.md` at 16:52, 2026-09-18.
**Status:** Recorded.

Four nodes. Every one `unreported`, every one `stale`:

| session | state | last spoke | age |
|---|---|---|---|
| update-claude-md-docs | unreported | 13:21 | 3h30m |
| solid-implementer | unreported | 04:45 | 12h07m |
| agent-interface-improvements | unreported | 07:49 | 9h02m |
| chief-of-stuff-improvements | unreported | 13:23 | 3h28m |

Both readings are correct and both are worth having. `unreported` is not a fault in those sessions: the live tracker is still the seven-column shape, so no `state` cell exists for any of them to fill. The graph is reporting, accurately, that the coordinator has not restarted onto a version that asks — which is the same fact as `CLAUDE.md:172` and the unread 0.7.0/0.8.x installs, arriving here on its own without anyone going to look for it.

The ages are the older argument, now visible. Half a working day of silence from `solid-implementer`, and until this render nothing anywhere on the board said so. Every one of those rows had been read all day as a statement about the present.

The thing to watch after the restart is whether these four go to a written state or stay `unreported`. If they stay, the poll is not landing, and that is a ruleset problem rather than a renderer one.

## 65 — the board reported six and drew four

**From:** Zach on the live board ("That's unintelligible. no formatting."), relayed by `gauntlet-bc` 2026-09-18 16:54, with a correction from it at 17:00.
**Status:** Adopted 2026-09-18.

Three faults under one complaint. The reported one — six fields joined with ` · ` into a single inline run — was the least of them. Two fields were not on the page at all.

**`re-arm`, dropped since 0.6.0.** `RESUME_STRIP` was an allowlist of four names. `dd16fb5` added `Re-arm` to the ruleset as a Resume field on 2026-09-17 and did not add it to the renderer, so from that commit forward the field was written every move, parsed every render, counted in `resume=N fields`, and drawn never.

**`verified`, dropped for one hour.** `BULLET` splits at the first colon, and in `- Verified 16:52: main = …` that colon is inside the clock read: the key parsed as `verified 16`. The correction from `gauntlet-bc` matters here and I had it wrong in the commit — this is not old. That spelling was introduced at 16:52 **by the rewrite meant to fix the legibility complaint**, so the repair removed the contradictory numbers and removed the line. One hour, not a day, and self-inflicted by the fix.

**What made both survivable is the same number.** `resume=N fields` counts what was parsed, never what was drawn. `gauntlet-bc` read `resume=6 fields` as confirmation the block was intact, which is exactly the reading it invites. **A board that reports six and draws four cannot be caught by reading its own output**, and no amount of attention to that line would have found either bug. The peer's own verdict, and it is the right one: making those two numbers the same is a better fix than either bug under it.

Generalised, this is the rule the renderer's summary line now runs under: **a count of inputs is not a count of outputs, and only the second one is a check.** `lanes=`, `requirements=` and `resume=` are all counts of what was read. Where a filter, an allowlist or a parse can sit between reading and drawing, the count that belongs on stdout is the one taken after the filter.

Second, smaller: `resume_long=4` printed beside `warnings=0` for three hours while the block degraded, and was read past every time. A number printed next to a warning count but not counted as one reads as telemetry. It counts in `warnings=` now, with malformed session rows.

Third: the fixture wrote `- Verified:` with no time and the live tracker writes `- Verified 16:52:`. Every unit test passed throughout. The fixtures are anonymised copies of live shapes and they had drifted from the shape the ruleset now asks for — which is the same class of gap as finding 53, and the reason bullet 1's four corrections came from running the parser against the live tracker rather than against the fixtures.

## 66 — the audit could only reach a tree through a lane

**From:** running bullet 3's join against the live workspace, 2026-09-18 17:20.
**Status:** Adopted 2026-09-18.

Finding 47 asked for one line: a tree with uncommitted work whose owner is not in the session list. I wrote it, the unit tests went green, and the live run reported `orphaned=0` — against a workspace holding exactly the three trees the finding was written about.

`audit()` walked `for lane in lanes: rows_for(lane.item, lane.owner, owners)`. Every tree it ever looked at was reached through a lane that claimed it. The three orphaned trees are keyed in File ownership as `nobody (session TTL fix, orphaned …)`, and no lane owner is `nobody`, so `rows_for` matched none of them and the sweep walked past all three.

**A tree reachable only through a lane is a tree nobody can reach once the lane lets go** — which is the same event that orphaned it. The structure guaranteed that the trees most worth finding were the ones the traversal could not see.

Fixed by sweeping every File ownership row that names a tree after the lane pass, claimed or not. A row with no lane behind it reopens nothing — there is no lane to reopen — but its git state and its owner are reported like any other. The live run now names all three, with branches and file counts.

The general shape is worth keeping, because it is not about worktrees: **a traversal keyed on a relationship cannot see the objects whose defining property is that the relationship is broken.** Finding 47's own phrasing had it — "it reports per tree and this is a fact about owners" — and I implemented the fact about owners while leaving the traversal keyed on lanes.

## 67 — CIMP: what the audit can check and what it cannot

**From:** Zach 2026-09-18 17:18, relayed by `gauntlet-bc` and confirmed against workspace `CLAUDE.md:59`.
**Status:** Adopted 2026-09-18.

House rule 5: "CIP now has a CIMP variant - MERGE. Work isn't done until it's in main. We should be testing final tests on main each time." A green branch is not evidence; a green main is, because a merge can break main without either side's branch suite noticing.

This changes what `done` means, and `done` is `audit_lanes.py`'s whole subject. "On main, committed" is now necessary and not sufficient.

**No script can watch a suite run**, so the audit does not pretend to. What it can do is name the commit the claim has to be about. `_push_gap` now prints `main <sha> even with origin/main`, and the ruleset requires that sha in `Verified` beside the suite result. When main has moved on, the evidence is about a main that no longer exists, and the mismatch is visible without trusting anybody's memory.

That is the same shape as finding 61: report the state and where it can be checked, not the conclusion. An unpinned "suite green on main" is unfalsifiable; pinned to a sha it is a claim that can go stale in public.

The stored `CIP` memory has been rewritten to carry the merge and the on-main suite. It was relayed to me as *contradicting* the new rule; Zach corrected that at 17:25 — **"CIMP is not contradictory with CIP. CIMP is an addendum rule"** — and the memory now says so. Worth keeping as its own small lesson: the relay was accurate about the rule and wrong about its relationship to the old one, and I had already written "contradicted" into a finding on the strength of it. I verified the rule itself in `CLAUDE.md` rather than taking it from the relay, which is the only reason it was safe to act on at all; I did not extend that check to the framing that came with it.

**A worked example, from `agent-interface-improvements` at 17:25.** It completed the first full CIMP cycle: merged S2 to main at `7f57092`, ran the suite on main afterwards, 436 passed, evals 32 of 32. The audit reports that lane "on main, committed" and would let a coordinator call it done — but what makes it done under the new rule is the run on main, which exists only in a chat message and nowhere the audit or the tracker can see. The gap observed rather than predicted, which is why the sha pinning is the fix and not a "did it pass" field nobody can check.

**And a trap to stay out of.** That same session found that regenerating the eval artefact on main always produces a diff — a generated-at timestamp and a runtime delta of a few milliseconds — so `report.md` and `evals.json` read dirty after every regeneration even when no result changed. Any check that treats "tree clean on main" as evidence of a completed CIMP gets a false negative from that, every time. The audit does not make that check and should not acquire one: `main <sha>` says where main is, and the suite claim is pinned to it.
