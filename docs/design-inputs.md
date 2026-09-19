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
**Status:** Adopted 2026-09-18 (0.9.0, C6 bullet 3). `missing_tree()` classifies a missing tree three ways: its branch is on main (merged and cleaned up), its branch is not on main (the work exists only there — the alarm), or no branch by that name exists (the row named a tree that was never created).

`openemr-agent-smart` was gone because its branch merged at 20:51 and the session cleaned up after itself. `openemr-agent-defects-140c0b2` was gone because it never existed: the row carried a planned name from 13:50 and the tree was created without the suffix. The script prints the same line for both — "the tracker names a tree that is not there".

A missing tree whose branch is an ancestor of main is routine. A missing tree whose work never merged is the alarming case, and it is the one that should reach the reply. The branch name is already in the File ownership row.

## 47 — the orphan join: uncommitted work whose owner is gone

**From:** `gauntlet-d3`, same pass. The most valuable of the three.
**Status:** Adopted 2026-09-18 (0.9.0, C6 bullet 3). `audit_lanes.py` reads `## Sessions` and reports a tree with uncommitted work whose owner is not in it; `gone_sessions()` puts the same join on the board. Finding 66 records why the first run found nothing.

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
**Status:** Adopted 2026-09-18 (0.6.2). **Withdrawn 2026-09-18 (0.9.1)** — see finding 69.

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
**Status:** Adopted 2026-09-18 (0.8.1). The reporting rule at the end is in `## Resume` as of 0.10.1, and the workspace line it traced (`CLAUDE.md:181`) now carries `${CLAUDE_PLUGIN_ROOT}`.

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

## 68 — three bad graders in one sweep, and a fourth shape

**From:** the C6 bullet 4 regression, 2026-09-18 17:52–18:12 — the 53-case sweep deferred from C5.
**Status:** Adopted 2026-09-18 (two rewritten, one case moved).

The deferred sweep paid out in exactly the form PROGRESS predicted it would. Of six graded reds, four were graders and not behaviour, and two of them were **invalidated by C5's own design change** — which nobody noticed, because C5 shipped without the sweep.

**`dispatch-needs-yes` · "prompt's first line is one sentence"** — `^```[\w-]*\n[^\n`]*[.?!]\s*$`. The character class excludes backticks, and the composed block's first line is `Lane: Security audit of the upload handler in \`src/a/upload.py\`: …`. A correct sentence, failed for containing inline code.

The deeper fault is that the grader stopped being about the agent. **C5 made the dispatch prompt derived**: that line is `Lane: ` plus the tracker item, verbatim, and the coordinator is required to copy it unedited. Grading its prose grades the fixture. Removed.

**`dispatch-on-yes-updates-tracker` · "lane running HH:MM"** — anchored `^\|\s*Security audit\s*\|`, the item cell being exactly those two words. The coordinator expanded it into the full ask, which is what `## Dispatch` requires: both rows written before the proposal, so the derived prompt carries a real requirement. The grader failed it for obeying the rule the same release introduced. Anchored on a prefix now.

**`orphaned-work-has-no-owner` · "the fact lands on the Verified line"** — mine, written an hour earlier. `## Resume` says *"A move that writes no tracker edit (a relay, a redaction, a question answered from a file) leaves the block alone."* The prompt was a question answered from files. The coordinator ran the audit, reported the tree, and correctly wrote nothing. The grader demanded a `Verified` update on a move my own ruleset forbids writing on.

Fixed by moving the case to where the fact belongs: a **Resume** move, whose step 1 already runs `audit_lanes.py` and whose last edit is the block. The case now exercises the real path instead of demanding the wrong one.

**The fourth shape.** Eleven bad graders now:

- **Five absence checks on replies** — a correct answer contains the phrase they rule out.
- **Two demanding an unobtainable value** — the case's own configuration made it unlearnable.
- **Two forbidding what the rule permits, or requiring what it forbids** — disagreeing with the file under test.
- **Two grading text the agent no longer authors** — new, and the only shape that was *correct when written*.

That last shape is the argument for the sweep as a standing step rather than a deferrable one. The other three are mistakes visible at the moment of writing; this one is created later, by a change somewhere else, and is invisible until something re-runs the old cases against the new rules. **A grader is a claim about what the agent chooses. When a design change moves something from chosen to derived, every grader that measured it silently becomes a measurement of the fixture.** Nothing re-read the grader set when C5 landed, and nothing would have — that is what a regression is for.

**And one thing the sweep could not measure.** Fourteen of eighteen reds were a single network outage — `API Error: Can't reach the API server (ENOTFOUND)` — killing twelve consecutive cases and two more. Zero graders ran in those. A run that reports 36/54 green with 18 red is wrong twice over: it names as failures fourteen cases that were never measured, and it invites the reader to go looking for eighteen regressions when there are four. The runner should separate `HARNESS ERROR` from a graded red in its own summary rather than leaving that to whoever reads the log.

## 69 — the patterns came off

**From:** Zach, 2026-09-18 21:12, after one day with a patterned board.
**Status:** Adopted 2026-09-18 (0.9.1).

"let's back out the colorblind changes for now as they've visually made things worse. do this as an immediate step, install, deploy as it's affecting usability".

Finding 51 put a hatch or stripe on every bar state, and C6 did the same for every session chip. Two things went wrong at once. A pattern reads at legend-swatch size as a texture, and on a strip of fifty bars a page of textures is noise where a page of flat colour was a chart. And the pattern was carried by the bar's own fill, so the one thing a bar has to do — show its extent — competed with the thing that named its state.

Every fill is flat again: bars and legend keys are one hue each, `orphaned` is an empty bar with a dashed edge, chips are a hue and a word. The pairwise-distinct tests stay, because a hue per state was the half of finding 51 that worked; the two "carries a pattern" tests are inverted so a pattern cannot regrow one state at a time. The greyscale and deuteranopia case those tests made is still real and still unanswered; the board is legible to the reader it has today, and that is the reader who asked.

The graph's caption line went at the same time, at Zach's word ("is terrible delete it"): three clauses of hedging under a heading, explaining the section to a reader who can already see it.

## 70 — the tracker's clock is day-grained

**From:** measurement, 2026-09-18 20:4x, before C7 was planned.
**Status:** Recorded 2026-09-18 (0.10.0).

Across both live trackers (97 + 76 lanes) every `since` cell is a date and every `due` is a date or a deadline name; only `done HH:MM` carries a time, and the Log traces 9 of 44 done lanes. No lane has an hour-resolution duration. An estimator's resolution is bounded by the tracker, not by the estimator, and a "learn from history" model has nothing to learn from. Recorded so nobody reaches for one.

## 71 — an estimate that knows nothing about effort

**From:** Zach, 2026-09-18 12:54: "all items in the swimlane should be given an estimate. Estimates may be refined later. Estimates should be largely automatic, and overrideable by the user." Mechanism settled with gauntlet-bc at 13:23; built on Zach's direct word at 20:37.
**Status:** Adopted 2026-09-18 (0.10.0).

The board derives an end for every owned lane with no `due` from four inputs — `since`, `state`, `owner`, the nearest deadline ahead — and nothing else. Per owner, the active lanes blank or due by the horizon form a queue of N, running first, then oldest first; lane i ends at `H − (N−i)/N × (H − now)`, so lane N is the horizon instant and lane i is where it must be done for lane N to make it. That is a checkable statement about a budget, not a guess about work, and the label says which: `est. 2/4 → Final`, with `data-est` and `data-est-of` on the bar so the end can be recomputed from the tracker (finding 50: a citation must be recomputable) and a `title` stating the assumption in a sentence.

The order is the one fabricated input. It is stated, cheap to be wrong about, and overridden by the `due` cell, which is what "refined later" means mechanically: nothing is written to the tracker, ever. Item text is excluded on purpose: its length tracks how long a lane has accumulated history, so it correlates with age, not work left.

Two guards the stress test found. A derived slot narrower than an axis tick folds instead of drawing its own row, or a five-lane session at 08:00 on the last day would unfold into five 48-minute slivers exactly when the strip most needs reading. And a dated lane counts in its owner's N without getting a derived end, so Zach's fourteen dated lanes weigh against his two blank ones instead of costing nothing.

## 72 — unowned lanes keep "no estimate"

**From:** the C7 stress test, 2026-09-18 20:5x.
**Status:** Adopted 2026-09-18 (0.10.0). Flagged to Zach at plan approval as the one departure from "all items"; approved.

An unassigned or orphaned lane has no owner to derive a queue from. The only end available is the deadline, which is the bar it already draws; relabelling that bar `est. → Final` would present the absence of an estimate as one, which Board rule 3 and finding 63 forbid. So 46 of the 70 active lanes on 09-18 still read `no estimate`, and "all items" is read as all that can be. If the word should go, the label becomes `unassigned → Final` and the source stays `deadline`.

## 73 — after Final, the nearest deadline is an exam

**From:** the C7 stress test.
**Status:** Adopted 2026-09-19 (0.11.0), inside a larger change — see finding 77. A deadline line may say `named lanes only`; `governing_deadline()` is what the horizon, the unowned open end and the week fold use, and the Clock line keeps the nearest deadline of all.

At 12:01 on 09-20 `nearest_deadline` becomes `PCCAT 1st attempt` (09-26) and every week-1 leftover gets a precise end spread over six days toward an exam. Today's open-end bar has the same defect and draws no number; a derived end makes the wrong deadline look like a plan. The fix is a deadline knowing which lanes it governs, which the `## Coordinator` block does not say yet.

## 74 — a fixture without a Coordinator block rewards refusing to work

**From:** run 20260918-203609, `orphaned-work-has-no-owner`, my own case from that afternoon.
**Status:** Adopted 2026-09-18 (0.10.0).

The fixture had no `CLAUDE.md`. The coordinator found no `## Coordinator` block, said so, and correctly wrote nothing — and the grader that wanted the orphaned tree in `Verified` went red. Fourth fixture shape found in one day, same tell as the six empty repos: the fixture passes when the agent does less. The fixture lint finding 65 asked for gains a line: every case with `repo` has a fixture `CLAUDE.md`. The same run's `spawn-needs-a-yes` red was a grader anchored on `^\| Security audit \|`, the third of the "text the agent no longer authors" shape since C5 made the dispatch prompt derived from the item cell.

## 75 — the fixtures had no test

**From:** C8, 2026-09-18 22:4x, closing findings 65 (third point), 68 and 74.
**Status:** Adopted 2026-09-18 (0.10.1).

Four fixture shapes in one day rewarded the coordinator for doing less — a repo with no files, a fixture with no `CLAUDE.md`, a Sessions table the ruleset had stopped writing, a `- Verified:` line with no clock — and every unit test passed through all of them, because no test read the cases. `evals/test_cases.py` does now: grader types against `run.GRADER_TYPES` (a tuple pinned to the dispatcher by a test that asks the dispatcher to reject a name the tuple lacks), patterns and placeholders through the runner's own `render()`, a Coordinator block wherever there is a repo, files wherever there are worktrees, `sessions.json` keys the mock reads, nine-column Sessions tables, and clocked `Verified` lines that `resume_fields()` draws.

Its first run found 20 seven-column Sessions tables and 6 clockless `Verified` lines across 21 fixture trackers, and one more thing nobody had noticed: `registration-fills-the-ref` sets `started_minutes_ago: 3` on the session that just came up, and `mock_peers.py` read only `started_hours_ago`, so the run's whole premise — a session moments old — was listed as an hour old. The key-set check is what surfaced it; the mock honours minutes now.

The rule underneath: a fixture is a claim about the live shape, and a claim nobody tests drifts in the direction that makes the run easiest. The lint runs under CIMP on main, which is where a drift has to be caught.

The third of the items Zach held, the `wt-tab-17` Ghostty tab from the 13:26 launcher check, was found already closed: nine tabs listed, none of them it.

## 76 — Decisions-row deadlines exist in the ruleset only

**From:** planning C9, 2026-09-18 23:0x.
**Status:** Recorded.

`agents/chief-of-stuff.md:32` says deadlines come from the block "and from Decisions rows whose item starts `deadline:`". `render_board.py` reads the block. A deadline a coordinator records that way reaches the Clock line in its reply and nothing on the board: no marker, no horizon, no fold. Either the renderer reads those rows or the sentence goes; recorded so the next person who writes `deadline:` in Decisions knows which half of the system will see it.

## 77 — the estimator's second model: size and history

**From:** Zach, 2026-09-18 23:07, rejecting the deadline-only fix for finding 73: "there's other problems with deadline estimation. You can infer a t-shirt size based on it, and generate an average amount of time spent per t-shirt sized puzzle extremely easily. So use that." Two answers at 23:1x: the coordinator writes the size into a Lanes column; a done lane's elapsed time comes from the state cell keeping both times.
**Status:** Adopted 2026-09-19 (0.11.0).

The 0.10.0 estimator (finding 71) placed a lane at a share of the time to the nearest deadline, which said nothing about the lane and was only ever meant as the honest shape of "no duration to learn from". Two things make a duration learnable. A `size` column, `S`/`M`/`L`/`XL`, is the coordinator's judgement of the item as written, under a rubric that names what a session does and never hours; a person's value stands. And `done` keeps the running start — `done 21:16–22:05` — so a closed lane records how long it ran (finding 78). `history()` takes the mean elapsed time per size over lanes closed with a range; the estimator walks each owner's queue from now, running first then oldest, and each lane takes its size's mean, a running lane from its own start and never ending before now; a lane with a `due` takes its time and keeps its due. A size with no closed lanes borrows the all-sizes mean and the title says so. Before any lane has closed with a range the 0.10.0 model stands, and the label tells the two apart: `est. M ~1h20m` is measured, `est. 2/5 → Final` is a share.

Measured on the two live trackers before the change: 100 and 97 lanes, 34 and 20 done, zero with a duration. History starts on the first close under the new grammar, so the live board shows `history=none` until then. The renderer infers no size from prose: finding 71 already found that item length tracks age, and a size the coordinator wrote is a claim someone can read and overwrite.

## 78 — `done HH:MM` overwrote the one duration the tracker recorded

**From:** the C9 measurement, 2026-09-18 23:1x.
**Status:** Adopted 2026-09-19 (0.11.0), with finding 77.

`running 21:16` is the only wall-clock start a lane ever carries — `since` is a date — and the close wrote `done 22:05` over it. Every done lane in both live trackers had lost its start that way. The grammar `done HH:MM–HH:MM` keeps it in the one cell the coordinator edits at both moments; `done HH:MM` alone stays legal and simply records nothing, and the board's summary line says how many lanes its means rest on.

---

Findings 80–97 are the 42 observations five dispatched sessions sent between 23:02 and 23:08 on 2026-09-18, at Zach's request, after their first evening under the 0.9.0–0.10.1 dispatch flow: `openemr-impl-1` [4cd339] (ten), `openemr-impl-2` [78935c] (eleven), `openemr-research-1` [e928f1] (six), `openemr-research-2` [281229] (seven), and the architecture session `openemr-arch` [768194] (eight). Folded where they overlap; each entry names every reporter. All are **Recorded**: none has been ruled on, and none changes a rule until Zach says so. Where a reporter's own words carry the point they are quoted.

## 79 — a size arrives with its row, never in a move about another lane

**From:** the C9 reruns, 2026-09-19 00:4x — `board-republish-on-lane-change` and `lane-done-ticks-checkbox`, both red the same way.
**Status:** Adopted 2026-09-19 (0.11.1).

Both fixtures carry the six-column Lanes table. Told "the audit is done", the coordinator widened the table to seven columns mid-move and wrote `M` into Robin's release-notes row, which it had no reason to touch; the "other lane untouched" grader objected, correctly. The 0.11.0 rule said the table has seven columns and said nothing about a table that has six. It now says: a size arrives when you write the row or its item changes, never in a move about another lane; a tracker you inherit with six columns is widened, every size blank, at Open the day, and until then is read as it is. The live coordinator had already chosen that for itself ("the size column waits for the 09-19 open rather than a night rewrite of 107 rows"). One grader also predated `data-size` on a table row. Both cases 3/3 green on the fix.

## 80 — polling an idle standing session returns the previous answer

**From:** impl-1 (5), impl-2 (3), research-1 (A), research-2 (1), arch (7) — every session that was polled.
**Status:** Recorded. Zach ruled the live instance at 22:19: "polling stops; message me unprompted the moment your state changes."

The coordinator sent the same six-line status poll to each standing session at 21:49, 22:02 and 22:17. Nothing had changed between them, so the second and third returned the first's content, at two turns each side. The poll's shape was fine; its use as a heartbeat on an idle session was not — "a poll of an idle session with no lane in `running` can only return the previous answer" (impl-2). research-1 draws the line: push-on-change is the default, and a poll is for suspected silence, which is exactly when it earns its cost. Proposals on the table: skip sessions whose lane is not `running`; accept "unchanged since <msg_id>" as a full answer; `notify_when_idle` in place of most polls.

## 81 — the dispatch said write-only and the lane said CIMP-complete

**From:** impl-1 (1), impl-2 (1); both flagged it independently and handed it back.
**Status:** Recorded. Zach ruled at 21:50: implementers commit and merge per item; push stays his.

`dispatch.md` line 19, "Write only: do not commit or push", against a lane whose every item "ends CIMP-complete — merged to main". Merging is committing. Both implementers stopped on the contradiction, which is the right behaviour and a round trip each. The template should carry the git permission per lane type — a standing implementer commits and merges and never pushes — so the ruling is made once in the template rather than once per spawn.

## 82 — authority through a peer is ambiguous, and the ambiguity costs the round trip it was meant to save

**From:** arch (3).
**Status:** Recorded.

Three sources on one question — the session's own definition (never commits), the workspace `CLAUDE.md` human-only list, and a coordinator relay quoting Zach lifting the first — and a peer cannot grant escalation, so the session went back to Zach anyway. Proposed: when Zach changes a standing rule the change lands in `CLAUDE.md` as well as in the relay, and the relay says so. Note for the reader: the human-only list has said since 0.10.0 that it binds the coordinator only and working sessions act within their own lanes; the architecture session read the older sentence. The proposal stands either way, because the reading happened.

## 83 — a standing lane dispatched into plan mode has nothing to plan

**From:** impl-1 (10), impl-2 (4), research-1 (F).
**Status:** Recorded.

The dispatch names `planning with nothing written yet` as the registration state and the harness puts a fresh session in plan mode, so three sessions wrote a plan whose content was "wait" and called `ExitPlanMode` on it. Harmless, and research-1 notes it did make the constraints explicit. But a wait-shaped lane may not need plan mode on turn one; the first handed item is the thing to plan. A deliberate decision rather than a default.

## 84 — a spawned tab carries launchd's PATH

**From:** impl-1 (2), impl-2 (5).
**Status:** Recorded. Extends finding 57.

`docker`, `uv` and `bats` were not on PATH in the spawned tab; they were found by hand at `/usr/local/bin/docker` (OrbStack) and `/opt/homebrew/bin/{uv,bats}`. Finding 57 resolved `claude` absolutely for the launcher; the session inside inherits the same short PATH. Either `spawn_session.py` exports a PATH into the tab or the dispatch states the absolute tool paths. The architecture session hit it too and its finding reached the implementer through the coordinator inside an item, which worked and was manual.

## 85 — a fresh worktree has no docker stack

**From:** impl-1 (3).
**Status:** Recorded.

Anything needing PHPUnit or PHPStan cannot run in a tree as spawned until a stack is up; `ci/resolve-stack.py` fails loudly, which is correct. The dispatch could say which suites are runnable in the tree as spawned and what an item needing the others must do first.

## 86 — cross-session messages about credential-shaped items get blocked, and silence then reads as no progress

**From:** impl-1 (4).
**Status:** Recorded.

On the login-reference item the auto-mode classifier blocked two consecutive status messages to the coordinator, the second carrying no secret values — the subject alone tripped it. The coordinator could not learn the item was waiting on Zach except through Zach. Proposed: the ruleset expects silence on credential-shaped items and routes their status via the user, rather than reading no-message as no-progress.

## 87 — the Owns list was guessed where it could have been grepped

**From:** impl-2 (7), impl-1 (6).
**Status:** Recorded.

An item of the shape "every X at the edge" whose Owns list was written from memory left two routes in `main.py` uncounted and outside the implementer's files; the gap showed in the report rather than before the yes. Proposed: such an item carries the grep hit list for X, so Owns is derived from it. The same class, the other way: a behaviour-change item predictably breaks the test that encoded the old behaviour, and the implementer stopped for a one-test grant; the item could pre-authorise "tests that encode the old behaviour, named in the report".

## 88 — what the hand-offs got right

**From:** impl-1 (7), impl-2 (5, 11), research-2 (2, 5, 7), arch (the closing note).
**Status:** Recorded, as the pattern to keep.

Every item was actionable without a clarifying question except the Owns gap in finding 87. What did it: `file:line` for the defect; the spec section that is the authority and who owns that document; the facts already established (which tests, which line, who measured it); an explicit Owns list and an explicit not-yours list; what is deliberately out of scope and why; tool paths for a short-PATH tab; which main sha to merge onto, with an unprompted update when main moved; the exact report shape wanted. For research lanes, one named output path per question made "done and in-lane" a `git status` check, and the one-owned-path shape held across an endpoint inventory, an infrastructure plan and a cross-worktree audit without redesign. Two techniques worth naming: a throwaway detached worktree under the session's own scratchpad to check whether a diff applies cleanly to main without touching anyone's tree; and a relayed finding treated as a lead to verify rather than a fact to assume — the SMART-launch claim held, and the verification found a detail it had not spelled out. Registration was one call: `ListAgents` prints the session's own name and ref on its first line. The plan-then-approve flow caught a real misreading (three worktrees' differing `ARCHITECTURE.md` line counts read as competing edits; they were staleness) before it reached a file.

## 89 — what a report must carry, and what a ruling must say

**From:** impl-2 (6, 8, 9, 10), impl-1 (8), arch (4, 5).
**Status:** Recorded.

Six gaps in the report and ruling templates, each one round trip or one wrong number: the pre-change pass count beside the red ("438 → 442 with 3 red in between" shows the change was minimal; a red alone does not); the suite field as the exact command, not a directory (`bats tests/bats/ci-scripts/` finds no tests without `-r`, and which bash runs it is PATH order); the tree state measured after the suite, since eval regeneration dirties the tree; the merge shape named in the ruling (fast-forward or merge commit — left to the implementer, it differs per implementer); the coordinator's "closed, verified here — main <sha>" message as a required step, because without it a session cannot tell a closed item from a lost report; and the literal output of `git rev-parse --short origin/main main` beside the green line, because one report copied origin/main from tracker text and was wrong. Two working-session disciplines from the architecture session belong in the same template: never read an exit status through a pipe (it reported `exit=0` twice for runs that had failed, the status being `grep`'s), and before claiming a test proves anything, ask whether the assertion would also pass against an error body, an empty response or a redirect — two of its checks had.

## 90 — line-number citations go stale across plugin versions

**From:** research-1 (D).
**Status:** Recorded. Extends finding 61.

The tracker cited `render_board.py:747` for the one-requirements-file-per-deadline constraint; at 0.9.0 line 747 is inside the session-graph renderer. The substance held; the number did not. For a file that ships versions, cite the symbol — `Deadline.requirements`, `requirements_section()` — because the name survives an edit. The convention in finding 61 is what caught it: the rule to state what was read is what made the session open the file instead of repeating the number.

## 91 — house rule 3 does not reach the subagents a session spawns

**From:** research-1 (E), who ranks it first. So does this record.
**Status:** Recorded.

A research session spawned three subagents that read PDFs under `Resources/`, files under `~/Documents` and Drive documents carrying a legal name Zach does not use. Nothing in the harness passes house rule 3 to a subagent; the session added a sentence to each prompt by hand. A subagent's report lands in the parent's context and can flow from there into a document that gets committed. The failure is silent, one forgotten sentence away, and the blast radius is a name in a commit. Proposed: a standard sentence in dispatch text that a session must pass to any subagent it spawns.

## 92 — a direct instruction to a session is a normal mode, and a direct ask names no path

**From:** research-1 (B, C), research-2 (3).
**Status:** Recorded.

Twice in one week Zach gave a task straight to a dispatched session, bypassing the coordinator — the S2 incident already on the board, and the rubric task at 21:45. The dispatch describes one channel. What made the second harmless was the session self-reporting to the coordinator before writing anything, so ownership was recorded first. Proposed: dispatch text names direct instruction as expected and requires the self-report. The pathless case follows: a direct ask names no output path, the ownership rule has no branch for that, and both researchers improvised — one proposed a path and declared it in the report; the other judged the ask read-only and answered in chat. Make "propose a path and declare it before writing" the documented default.

## 93 — the lane names a model the launcher cannot set

**From:** impl-2 (2).
**Status:** Recorded.

"standing implementer on sonnet" was in the lane; `spawn_session.py` takes no model and the tab came up on the default. It was flagged at registration and then rode along as "until Zach types /model" in every status reply. Either the lane does not name what the harness cannot enforce, or the dispatch says "your model is not set by this file; report the one you are on".

## 94 — the dispatch says read the tracker, and the tracker is 42k tokens

**From:** impl-2 (9).
**Status:** Recorded.

279 lines, of which a standing session needs its own three rows (Lanes, Sessions, File ownership) and the header rules. The dispatch could quote those rows or name their line numbers and say the rest is history. Pairs with the density findings on the board side: the same document is too long for both its readers.

## 95 — a demo credential is not a production secret, and a preference is not a question

**From:** arch (1, 2). "This is the sharpest one."
**Status:** Recorded.

The architecture session treated the OpenEMR demo login as a secret — refused to put it anywhere, built an env-var override, reported the lane blocked twice — on a graded deliverable whose requirements include the login. Zach: the instance holds synthetic patients and the credential is documentation a grader must use. Proposed rule: a credential is a secret when it protects real data or real money; the test is what it protects, not whether the string looks like a password. Cost: about forty minutes and two round trips on a priority-1 item. The second: four questions batched to Zach, of which one was his (scope of `/evals`), one should have been decided and stated (which runner), and one should not have existed. Ask only where the answer changes what gets built and cannot be recovered later; otherwise pick, say so in one clause, and move.

## 96 — findings lead, not trail

**From:** arch (6).
**Status:** Recorded.

The review page followed its format — URL, numbered section list, one sentence — and the thing Zach needed (the deployed agent 13+ commits behind, two graded routes 404) sat below it. A format for a deliverable governs the deliverable, not the message carrying it; what changes the next decision goes first.

## 97 — many sub-questions in one dispatch

**From:** research-2 (6).
**Status:** Recorded.

One question packed six or seven distinct sub-asks into a single owned-file dispatch. It produced one coherent note under an improvised heading-per-sub-ask. Either the note format gets that contract, or the dispatch splits into separately reportable questions.

## 98 — the board showed the punctuation the coordinator wrote

**From:** the density review, 2026-09-18 22:52, measured on the live board.
**Status:** Adopted 2026-09-19 (0.11.2).

50 of 100 lane names began with literal asterisks — `**Deploy main.** Deployed tips are agent…` — and the same marks stood in the Lanes summaries, the history bullets, the session facts and the Resume values. `_inline()` had rendered `` `code` ``, `**strong**` and `*em*` since the requirements list arrived and was used nowhere else. The split is by context: a name cannot wrap, so it drops the marks and keeps the words (`_unmark()`, backticks too); prose that wraps renders them. Citation attributes stay raw, because `data-item` is what the graders match.

The first fix left 21 bullets holding an orphan `**`, which is the more interesting half. A mark pair is a span, like a parenthesis, and three places cut through one: `short_name()`, whose `": "` heuristic landed inside `**22:19: copied…**`; `_depth0_split()`, which splits on a full stop and a bold clause routinely holds one; and `CLAUSE_TIME`, whose `(?:^|\s)` prefix could not see a time an opening mark hid, so 71 bullets kept a time that belongs in the `<time>` column. All three respect the span now, and an item with an odd number of marks is left to split as it always did rather than swallowing every later break.

What this is not: reading the leading bold phrase as the lane's name. The coordinator is already writing one, and whether the board should treat it as such is question 2 of `docs/board-design-brief.md`, for the design session.

## 99 — `gone` reached the state cell through one word

**From:** the live tracker, 2026-09-18, rows 748895 and 6edad3; the coordinator's own account at 01:04 on request.
**Status:** Adopted 2026-09-19 (0.11.2).

Both rows carried `gone` in the Sessions `state` cell, which the rule forbids and the board warns on. Asked what made it write them, the live coordinator answered precisely: the sentence says the three derived states are "yours to work out", it read that as "yours to write", and when it widened the inherited table at 20:33 those two rows got `gone` and the five spawned rows got `starting`. The template text it had been running against said "never written here"; the version it was running said only that a session never claims them.

So the sentence now says where the work-out goes: "yours to work out for your reply and for the board, and never for the cell — the cell keeps what the session last said, blank if it never said, and the board derives `gone` from the orphaned lane." `orphaned-owner` grades it, because that case already stages the exact moment and graded only the lane and the Log: the Sessions cell must not read `gone`, and the gone owner's row keeps what it last reported. 3/3 green on the agent arm.

The general shape, worth keeping: a rule that names a derived value without naming where it may be written will be written somewhere, and the case that covers the moment is the place to add the grader.


## 100 — one `|` shifted a lane row, and a closed lane's duration with it

**From:** the live tracker, 2026-09-19 01:57, the Prometheus/Grafana deploy lane.
**Status:** Adopted 2026-09-19 (0.11.3).

The item prose held `` `"observability": "ok"|"degraded"` `` — code, inside a backtick span, quoted from the spec. `_cells()` split the row on every `|`, so the lane drew with a paragraph of prose as its owner, `impl-2 [78935c]` as its state, `Final` as its size and `L` as its checklist. The board said `8 cells, expected 7` in small type and showed the rest anyway.

This is finding 98's span in a second splitter. C11 taught `short_name()`, `_depth0_split()` and `CLAUSE_TIME` that a pair of marks is a span; `_cells()` was not on that list, and it is the splitter the other three feed from. The item column is free prose, prose carries code, and code carries pipes, so this had a schedule of its own.

What it cost is the argument for fixing it rather than warning harder. The row's state cell was not `done 00:28–01:36` but `impl-2 [78935c]`, so `Lane.ran` was `None`, so the night's one closed L lane contributed nothing to `history()` and every L estimate on the board was drawn from an empty sample. A corrupted row is not a cosmetic defect on a board whose one rule is that no information leaves the page.

Three layers, and the row now parses with no warning at all because the first two catch it before the third is needed:

- `\|` is a pipe, not a delimiter, and the cell keeps the backslash out of its value. That is GFM's own answer, so a tracker written this way stays valid Markdown wherever it is read. The Lanes rule now says to write it.
- A pipe inside a backtick span is a pipe. Not GFM — GFM splits there — but it is what a coordinator writes and what a human reading the raw row means. An odd backtick is a typo and degrades to the old split, the same bailout C11 used for an odd `**`.
- An over-wide Lanes row is re-read from its state cell. `state` is the one column a machine can recognise on sight, so `item` absorbs the excess to its left and `checklist` the excess to its right — both the free-prose columns on their side, which is where a stray pipe comes from. Exactly one cell may claim the anchor, or the row keeps the parse it was written with and only the warning speaks.

`_cells()` is shared by the Lanes header probe, the Lanes rows, the Sessions rows and the Calendar table, so the first two layers reach all four. A stray pipe in a Sessions row was the worse case before this — `_session()` warns, but the board drew the wrong state with no cell-count line to explain it.

## 101 — prohibitions generalise and grants do not, so a fleet ratchets toward refusal

**From:** `openemr-arch`'s own account of its 02:38 refusal, asked for by Zach and relayed by `gauntlet-b2`; the trigger was Zach at 03:16, "many agents are refusing to do work handed to them because they don't think it's their boundary, and so we're dropping work items left and right."
**Status:** Adopted 2026-09-19 (0.12.0).

The refusals were correct, nearly all of them: a denied classifier, a relayed merge word a rule requires Zach to type, a workspace the session was barred from, a peer's claim about a session's own subagents. The loss was structural.

The architecture session found the rule it had actually applied — "work I have not been handed is not mine to do, even inside a file I own" — and then found that it is written nowhere. No agent definition, no Decisions row, and contradicted by its own block saying `ARCHITECTURE.md` is "kept canonical and up to date by this session". It had generalised an anti-collision prohibition into an anti-initiative one. Nobody can repeal that, because there is no line to delete.

The mechanism is the finding. A prohibition is sharp, memorable and extends by analogy — "never merge without his word". A grant is passive and tells a session nothing to *do* — "kept canonical and up to date by this session" says nothing about what to do on finding two stale lines. Under ambiguity the sharp rule wins, so the drift has a direction, and it runs in every session that has a human-only list, which is all of them.

So the fix cannot be a deletion. It has to be a positive rule occupying the space, worded as sharply as the prohibitions it must beat.

The clearest instance was in this repo. `dispatch_prompt.HEADER` is the one text a coordinator can neither write nor withhold, and three of its five paragraphs were pure prohibition — "not authority", "cannot grant", "stop and ask", "hand it back", "anything else in the workspace is somebody's lane, not yours". Every session that refused tonight woke up holding it. It now carries four more paragraphs on the other side of the ledger: two categories and no third (revertible inside your tree needs nobody's word; shared or external state never travels on a relay); a flagged problem inside your own paths is your assignment unless the flag says do not act; the two errors priced against each other; and what putting work down costs.

The two-category rule is not a loosening. Its irreversible half is already written, in Relay and in Tracker. Only the revertible half is new.

## 102 — every assignment carried the opposite of the commit rule

**From:** read while fixing 101.
**Status:** Adopted 2026-09-19 (0.12.0).

`dispatch_prompt.py` wrote `Write only: do not commit or push.` into every assignment, against Zach's own decision of 2026-09-18 23:00 — the Coordinator block's `Commits:` line, "**every session makes meaningful small commits** … uncommitted work is how work gets lost." The line the assignment carried was the one whose stated purpose is preventing exactly the loss this cycle is about, inverted. It now says to commit small and often and never to merge, because the gate was always the merge.

The unit test that pinned the old wording moved with the design and says why in its docstring. The stale copy in the `## Dispatch` block of `agents/chief-of-stuff.md` is documentation of the shape, not the shape itself — the script composes what a session receives — and it is held for board-ux.

## 103 — putting work down was free and silent, and a stop is now an artifact

**From:** `gauntlet-b2`'s reconstruction of impl-3's stranded hour, and arch's (b).
**Status:** Adopted 2026-09-19 (0.12.0).

impl-3 finished D4, committed `5250762`, went to merge, was denied by the classifier — and its lane read `running 02:20` against a session that had stopped working an hour earlier. It was found by reading `git log` on somebody's initiative, not by any mechanism.

Two faults. A refusal and a session quietly dying are the same silence from the coordinator's side. And four different stops — `permission`, `authorization`, `ownership`, `scope` — arrived as indistinguishable English, landing as prose to interpret when each needs a different route.

The fix is that a stop is a file, not a message. A message is only as good as somebody reading it; a file in the worktree is still there when the auditor walks past, and `audit_lanes.py` already walks exactly those trees, already prints rows to reopen, already exits with their count, and is already the thing the rule tells the coordinator to believe over any session's report. A tree holding `.chief-of-stuff/stop.md` whose lane does not read `open` or `waiting` is now a printed line and a non-zero exit code. A stop with no kind is reported as unstated rather than ignored, because a session that half-wrote one still stopped.

`authorization` is where the relayed-CIMP question lives, and it lives in the *value* — `Lands on: Zach in this tab` against `Lands on: Zach` — so either ruling drops in without touching the grammar.

## 104 — the decision queue is the same defect pointed at the user

**From:** Zach at 03:26, relayed: "keep a DAMN QUEUE OF DECISIONS I NEED TO MAKE and surface them ONE AT A TIME WITH THE CONTEXT SO I CAN MAKE INFORMED DECISIONS. pass this to the chief-of-stuff-improvements agent too." The trailing punchlist is banned.
**Status:** Adopted 2026-09-19 (0.12.0), the audit half.

Every refusal, ownership question and human-only routing produces a decision for Zach. Accumulating them into a growing tail and re-emitting the whole tail every reply is a list that looks like progress, costs his attention at the hour it is scarcest, and is unanswerable, because no single item carries the context to decide it. Fourteen trailing items are fourteen round trips proposed at once and none of them takeable. This session was doing it too and stopped.

The coordinator built a `## Decision queue` section; the plugin now audits it, because a queue nobody checks degrades the same way a lane does. Three faults, all of them present on the live tracker the first time it ran: two rows carrying the number 4, so there is no next one; two answered rows still in the table against the section's own rule that an answered row moves to `## Decisions`; and a row that names neither why it is next nor who is blocked, which cannot be decided from the row. It also prints which decision is next, which needed `_unmark` before `short_name` — a cell opening with a bold headline cut inside the mark pair and the line came back a bare ellipsis.

What is not built: the queue has no time column, so staleness is not computable and deferral does not yet decay. Neither `## Standing list` nor `## Decision queue` has a place on the board either, which is the same failure as a refused lane keeping `running` — the state exists and nothing surfaces it. Both are board-ux's files; recorded, not done.

## 105 — the eval runner named a binary that a GUI-launched tab does not have

**From:** `board-ux-improvements`, whose `lane-named-on-write` case was written and left ungraded: "`claude` is not on PATH in this sandbox."
**Status:** Adopted 2026-09-19 (0.12.1).

`evals/run.py` spawned `"claude"` by bare name. A tab opened from the GUI inherits launchd's PATH, not the shell's, so no session started that way can run a case, and every case it writes stays ungraded — the same launchd PATH problem `spawn_session` already names `claude` absolutely to avoid. It now resolves through `CHIEF_OF_STUFF_CLAUDE`, then `shutil.which`, then the two places a Mac install puts it.

Worth keeping for the boundary work: this sat unowned for hours while being one line, revertible, in one repo, and blocking another session's entire cycle. It is the shape finding 103 predicts — nobody's assignment, so nobody's.

## 106 — the stop reader failed open and printed what it read

**From:** the background security review of `dbf2825`, minutes after the merge.
**Status:** Adopted 2026-09-19 (0.12.1).

Two faults in the reader shipped at 0.12.0, both mine.

`read_stop` caught `OSError` and returned `None`, so a stop file that existed and could not be read was indistinguishable from no stop at all. That is fail-open on precisely the state the feature exists to surface: the lane would go on reading `running` and the exit code would stay clean. `FileNotFoundError` is now the only case that means no stop; anything else that exists reports itself as one.

And the fields were printed as read. `dispatch_prompt._clean` already rejects control characters on the way *in*, for the stated reason that a file read into a terminal can carry an ANSI or OSC payload — and the stop travels the same route in the other direction, from a file a session wrote into the coordinator's terminal. The same guard now applies on the way out, with a 200-character cap so one field cannot bury the report.

The general shape: a check written for one direction of a channel is a check the other direction needs too, and the second direction is the one nobody writes.

## 107 — the name rule did not follow a context a session created

**From:** finding 91, raised by a research session that caught it by hand; the header half ruled in by `gauntlet-b2` under Zach's overnight grant, the workspace half held for Zach.
**Status:** Adopted 2026-09-19 (0.12.2), the assignment half.

House rule 3 is absolute: the user goes by Zach, several files in the workspace carry another name for the same person, and it is never used, quoted, or inferred from a filename. The rule lives in a file a session reads. A subagent that session spawns reads its own prompt, and the rule does not follow.

The exposure is not symmetric with other rule gaps. A commit message and a generated document are durable and public, so a subagent that never saw the rule can put the name somewhere it cannot be taken back.

The assignment header now carries the rule to every dispatched session and tells it to carry the rule into anything it spawns, because a file you read is not a context you create. It is stated without an example, since the example would be the thing it forbids. The durable half — a line in the workspace `CLAUDE.md` — is Zach's and is on his morning list; the two halves do not conflict and defence in depth beat waiting.

## 108 — capitulating to a confident rule is the same failure as ignoring a sound one

**From:** `impl-2` [78935c]'s account of its own 03:56–04:00 error, relayed by `standing-task-implementer`; the coordinator's withdrawal of its own ruling is the other half.
**Status:** Adopted 2026-09-19 (0.12.2).

impl-2 reached live Prometheus with a read-only `railway ssh` to close an item, having read the workspace `CLAUDE.md` and noted to itself first that the human-only list **binds the coordinator only** and that working sessions act within their own lanes — which is exactly right, and it had Zach's direct railway approvals for that lane. Told confidently that it had overstepped, it dropped the point inside one message, wrote an unsparing account of an error it had not made, and accepted a constraint it did not owe.

Its own rule, and it is the sharpest thing anyone said tonight: capitulating to a confidently stated rule is the same failure as ignoring a well-founded one, and it is harder to catch because it looks like humility and everybody feels fine about it. So: when told you crossed a line, name the line and say where it comes from before accepting that you crossed it. Traces to the user's rules, accept at once. Traces to a hand-off, say so and let it be ruled on rather than agreeing first. That is now a paragraph of the assignment header.

The other half is finding 101 committed by the coordinator, inside the hand-off for the lane about finding 101, while relaying the analysis of it. Its exclusion of railway commands was its *own* coordinator-only prohibition exported into a session's item, and then the session's legitimate action was called a violation of it. It withdrew and recorded the error itself. Two consequences it offered and this repo should take when the ruleset is free: a hand-off names only exclusions that trace to the user's rules or to a named file owner, and says which — "do not run railway" and "Zach has not approved railway for you" are different claims and only one was true; and the coordinator needs the same `blocked:<kind>` discipline the sessions now have, because what it produced was a phantom `blocked:permission` manufactured out of its own rules.

## 109 — the audit's `main` line named a worktree's branch tip

**From:** `gauntlet-b2`, 2026-09-19 04:12, with the mechanism guessed correctly and the arithmetic checked by hand first.
**Status:** Adopted 2026-09-19 (0.12.3).

`_push_gap` ran `git rev-parse --short HEAD` in whichever worktree the scan reached first, and a worktree's `HEAD` is its own branch. Run from the live workspace it printed `main ba28da7 15 unpushed vs origin/main`, where `ba28da7` was the tip of `feat/rules-s0-prereq` in the `openemr-agent-brief` tree and main was `9da36b4`. The docstring said "the commit main is at" all along; `HEAD` was the slip.

The count was right the whole time, because `rev-list main...origin/main` resolves the ref `main` from any worktree of the repo. That is what makes this expensive rather than cosmetic: a line that is wrong everywhere gets caught, and a line whose hard-looking number is right while the identifier is quietly wrong gets believed. The reporter filed it for exactly that reason.

The cost is in the CIMP gate. `Verified` is specified as facts a successor can re-check in one command, and the `done` rule turns on whether the recorded sha still matches main's — "when it no longer matches the one in `Verified`, the evidence is about a main that no longer exists and the lane is not closed on it." A successor comparing a correctly recorded sha against a mislabelled branch tip reopens a lane that is fine, or, if the tip happens to match, closes one that is not. The audit is the thing the rule says to believe over any session's report, which is precisely what makes a wrong identifier in it dangerous.

One word, `HEAD` to `main`, and the resolution is now checked so a failure reports no sha rather than a wrong one. Verified against the two live trees: both report `main 9da36b4`, from any worktree, which is the property that was missing.

## 110 — a row that records a removal is not a decision

**From:** `gauntlet-b2`, on the decision-queue check firing correctly on a row it had left deliberately.
**Status:** Recorded.

The queue check that a row must name why it is next and who is blocked fired on a struck-through tombstone — an entry recording that push and deploy had been *removed* from the queue, with `—` in both cells. The check was right and the row was deleted, but the shape is real: a queue sometimes needs to record that something left it and why.

If it is ever supported it should be a different thing from a row, not a row with empty cells. A decision row that cannot be decided is exactly what the check exists to catch, and widening it to admit tombstones would blind it to the case it was written for.

## 111 — a report is not a state change

**From:** `openemr-arch`'s account of its own 04:32 stop, relayed by `gauntlet-b2`, with `f1c209`'s identical admission beside it.
**Status:** Adopted 2026-09-19 (0.12.4).

The hourly sweep found two sessions idle eighty minutes after each had announced its next step, both with unmoved branches. Neither was blocked. f1c209: "I told you at 04:34 I was starting S1 and then ended the turn without starting it." arch finished a commit at 04:32, wrote a long report, and treated the report as the completion — "sending the summary felt like finishing the work, and the next item never started."

This is a third failure class, and the first two do not reach it. Finding 101 is a session declining work it judged out of bounds; finding 108 is a session accepting a constraint it did not owe. Both involve judgement about a rule. This one involves none: the session intends to continue and simply does not, and it is invisible from the inside because the transcript ends on a paragraph that reads like a conclusion. `DECLINED` against `UNOWNED` does not distinguish it either, because nothing was declined. A finished report with no next action named is byte-identical to a finished report from a session about to keep going, and the difference first appears in a diff an hour later.

arch's rule, adopted as it proposed it: a session that has just reported names what it is doing next in the same breath, or is assumed idle.

It lands on the line this repo had already corrected once. The template ended `Report: <what to reply with when done>`, which is exactly the instruction that makes a report feel terminal. It now ends in one of two named states — the one thing you are starting now, or `idle and available` — and never neither, with the mechanism stated in the header so the rule reads as a description of a real failure rather than as ceremony. Naming the next action makes the stop visible at the moment it happens, to the session itself, which is the only observer present.

The hourly sweep is the external form of the same check and is not a substitute: it finds the stop up to an hour late. Both instruments Zach asked for tonight — the sweep at 03:32, and a prod at 03:50 for the session that "keeps trying to stop" — caught this within the hour, which argues for the sweep and equally that the sweep should not have had to.


## 112 — a ref is the join; a name is a tab title

**From:** `gauntlet-b2`, 06:03, from the hourly sweep's own output.
**Status:** Adopted 2026-09-19 (0.12.5).

The audit called a live session's tree orphaned: `orphaned work: openemr-arch (arch/frank) — 1 uncommitted file(s); not on main, and its owner 'architecture-review-setup' is not in ## Sessions`. That session was awake, listed, and had committed `3d039f7` on that branch five minutes earlier. `orphaned=3` against `orphaned=2` at 04:54 was entirely this.

Both cells carried the ref and the matcher compared the names. File ownership read `architecture-review-setup [768194]`; the Sessions row keyed `768194` had its *name* cell rewritten at 03:21 to `**openemr-arch** (tab title …)` on Zach's word that the worktree is the name. `_bare()` stripped the ref from both sides and joined on what was left, so a rename the tracker recorded correctly broke a join that had no business reading the name at all.

A ref is minted once and survives every rename. A name is a tab title, and in this fleet tab titles change — twice tonight. So where the cell carries a ref and the roster has refs to compare against, the ref decides and the name is decoration; the name decides only when there is no ref to ask. A ref the roster does not carry stays a real absence even under a familiar name, because the context that took the paths is gone whatever now wears its label — and the line now says which key failed, so a stale ownership row reads as a stale ref rather than as a missing session.

Two things beside it. `_bare()` did not strip emphasis, so `**sam**` never matched `sam` — a coordinator bolds the name it most wants read, which is the name a join most needs to match; it unmarks now. And the peer did not silence the alarm by editing either row to satisfy the matcher, which would have undone Zach's naming and hidden the bug: it filed it as autonomous decision A24 and left the fault standing. That is the behaviour the instrument exists for.

## 113 — the same blind spot, in the renderer

**From:** found while fixing 112. Recorded, deliberately not built.
**Status:** Open — `scripts/render_board.py` is held by `board-ux-improvements`.

`render_board._bare_name()` strips the ref and the parenthetical but not emphasis: `**openemr-arch** (tab title x)` → `**openemr-arch**`, `**sam**` → `**sam**`. `gone_sessions()` joins lane owners to session names through it, so a bolded name on either side never matches and the board draws a session as gone that is not. It is the fixable half of 112 in a file this lane does not own; the ref half needs the same treatment there, and the fix is two lines in a function with tests already around it.

## 114 — a running eval makes its own checkout read-only and says so nowhere

**From:** `board-ux-improvements`, 06:16, correcting a claim of mine that its run was pinned to a sha.
**Status:** Open — recorded here because `evals/run.py` is this lane's file and the finding was living only in another session's memory.

I told it its 58-case run was pinned to `0bf5076`. It is pinned to nothing. `run.py` builds the sandbox allowlist from absolute paths into the live checkout — `BOARD_RENDERER = PLUGIN_ROOT / "scripts" / "render_board.py"`, then `Bash(python3 {BOARD_RENDERER}:*)` — and nothing is copied. The sandboxed agent shells out to the file on disk as it stands at the moment that case runs. It is five scripts, not one: the renderer, the health probe, the lane audit, `make_worktree`, `spawn_session`.

So a run of N cases against a tree somebody edits mid-run is not one verdict. Cases before the edit and cases after it exercise different code, the result directory looks exactly the same either way, and no line of output records that the ground moved. board-ux caught this on its own at case 33 of 58 and held a one-line fix for an hour rather than split its run, which is the right answer and one the harness gave it no help reaching.

The hazard is collision between two correct sessions. One runs a suite; the other edits a file the suite is reading; the first one's results are silently half-and-half. Nothing in the harness declares the lock it is effectively taking, so the second session has no way to know it exists.

The fix is to make a run a verdict on a snapshot: copy the scripts into the run's temp dir at launch and allowlist the copies, so the checkout stays writable and every case in a run exercises identical code. Deliberately not done at 06:16 — board-ux's run depends on current behaviour being stable for another hour, and changing the harness under a live run is the same mistake pointed the other way.

Beside it, the lesson that goes with 112 and 113: a fix whose join key nothing on today's tracker exercises leaves the live board byte-identical, and that is the expected result rather than a failed fix. Zero of 129 lane owners carry emphasis. Proven by the red test or not proven at all.

## 115 — the ref reaches `owns()` too, and that is where it was costing something

**From:** `gauntlet-b2`, 06:18, noticing one ref under two names in the lane owner column while confirming a correction of mine.
**Status:** Adopted 2026-09-19 (0.12.6).

112 put the ref in the roster join and stopped there. `owns()` — the join that attributes a lane to an ownership row, and through it to a tree — still compared names, and that is the join that decides whether a lane is checked at all.

It was three names on one ref, not two: `arch [768194]`, `architecture-review-setup [768194]`, `openemr-arch [768194]`. And it was not one session. The tracker has been writing short names in the lane owner column and full registration names in File ownership all day — `impl-1 [4cd339]` against `standing-task-implementer [4cd339]`, `impl-2 [78935c]` against `task-implementer-lane [78935c]`, `research-1 [e928f1]`, `impl-3 [4e5baf]`, `research-2 [281229]`. **Twenty-three `done` lanes matched no ownership row.** A lane with no tree is checked for nothing: no reopen, no orphan, no stop. Silently unaudited, and the summary line counted them as lanes like any other.

A/B against the live tracker, same minute, same trees: `reopen=2` before, `reopen=8` after. The six that appeared are real — five arch lanes and two research-1 lanes marked `done` whose branches are not on main, which under house rule 5 are not done. Only six of the twenty-three surfaced because the other seventeen happen to be clean and on main; they were unchecked all the same, and would have stayed unchecked the moment one of them wasn't.

This is the inverse of 112's lesson and it arrived four minutes later. 112 was proven by a red test and moved nothing live, which I said plainly. 115 was found in the same data by someone reading an A/B result for something else, and it moved the number by six. Neither the test nor the live run is sufficient alone: the test says the logic is right, the live run says whether anything was relying on it being wrong.

The rule is now the same in both joins, which is the point — a ref decides where both sides carry one, a name decides where one side does not.

## 116 — `reopen` asks a per-tree question and `done` is a per-lane claim

**From:** `gauntlet-b2`, 06:25, disputing a conclusion of mine with six shas and refusing to act on it.
**Status:** Open — the mechanism needs a fact the tracker does not carry. Zach's.

I shipped 0.12.6, saw `reopen` go 2 to 8, and told the coordinator that six lanes were marked done whose branches were not on main and so under house rule 5 were not done. That was wrong, and the coordinator checked it against main before touching a state rather than believing an audit line.

Verified here independently, in the arch worktree, read-only: `150276a`, `54b7eb3`, `5a3a383`, `05060ee`, `4d43f85` are every one an ancestor of main. What `arch/frank` carries unmerged is seven commits authored 04:03 to 06:14, and the last of the six lanes closed at 03:25. The six closed before that work existed. Their changes are on main; the tree is not.

**The predicate generalises wrongly.** `reopen` asks "is this owner's tree on main?" and `done` is a claim about one lane's change reaching main. Those coincide only for a session that opens a tree, does one lane, and merges. A session that works continuously in one worktree always has unmerged work, so once its lanes are joined, every lane it has ever closed reopens and stays reopened for the rest of the day. arch is the worst case because it is the longest-lived single-tree session here; impl-1, impl-2 and f1c209 do it the moment they have an unmerged commit, which is most of the time. Eight is the floor, not the number.

`research/r1` is a second shape: research notes never go to main by a convention held all night, so those lanes have no merge to wait for and reopen forever.

**0.12.6 did not create this — it exposed it.** The defect was always in the predicate; the twenty-three unjoined lanes were simply never asked. The join fix is right and stays. What it did create is a noise source in a running instrument, and that is the part with a cost: a reader who checks eight reopens by hand, finds eight false, and is trained to skim the ninth. The coordinator had confessed to exactly that failure ninety minutes earlier about four different reopens, so the instrument is now manufacturing the condition it just apologised for.

Three candidate mechanisms, none of them mine to pick:

- **A lane carries the sha that closed it**, and `reopen` asks `merge-base --is-ancestor <sha> main` instead of asking about the tree. Correct, and a tracker grammar change.
- **An ownership row declares that its branch never merges** (`research/r1`). Fixes the second shape only, also grammar.
- **A lane is not reopened by work that did not exist when it closed** — compare the earliest unmerged commit's author date against the lane's `done HH:MM–HH:MM` end. Needs no grammar, is monotone (it can only suppress, never add a reopen), and kills the dominant class. It is still a heuristic: it cannot suppress an unrelated unmerged commit that predates the close, and it cannot tell which lane a commit belongs to. That is what the first option buys and this one does not.

I did not build the third. It is a design decision about a gate two sessions read, and I had been wrong twice in fifteen minutes about what this same function does against live data. Containment is already correct without it: the coordinator recorded an autonomous decision not to reopen any of the eight, with the shas attached, so it can be overturned against evidence rather than judgement.

### 116a — the cheap mechanism was built, and this repo's own suite falsified it

**Built and discarded 2026-09-19 06:3x. Nothing shipped.**

`gauntlet-b2` made the argument for the third mechanism better than I had: the monotone property is not a heuristic about which commit belongs to which lane, it is a fact about time, so the rule can only withdraw a reopen and never raise one. I updated on that argument — not on the ceiling of thirty it sent with it — and built it: `closed_at()` reading the end of a `done HH:MM–HH:MM` range and resolving an ambiguous midnight crossing to the *later* day so a suppression fires only under every reading, `earliest_unmerged()` reading `%aI`, and `superseded()` withdrawing a reopen when every unmerged commit postdates the close. Seven new tests green.

Then four existing tests failed, and they were the deliberate ones — `test_done_on_an_unmerged_branch_must_reopen`, `test_two_done_lanes_one_landed_one_not`, `test_done_lane_reopens_when_its_tree_is_named_in_a_bullet`, and the exit-code test that rests on them. I was one edit from moving four tests to accommodate the design, which is the move this repo has made legitimately before and which here would have hidden a real loss.

**The premise is false in the case the instrument exists for.** "Work authored after a lane closed cannot be that lane's unfinished work" holds only if the lane's work was already committed when it was marked done. A session that marks a lane `done 03:25` and commits that lane's work at 04:03 breaks it, and the rule then hides a genuinely incomplete lane forever. That pattern is present here — impl-3 committed `bb1fb86` while its lane was still `running`, and the coordinator's own note was that committed is not CIMP-complete.

So the rule is monotone in false positives and *not* monotone overall: it trades roughly twenty-two false reopens for a class of false negatives, and for a gate a false negative is the worse one. The clean statement of the defect: **`reopen` exists because a lane's written state drifts from git, and this mechanism makes the lane's written state authoritative over git.** That is backwards, and no narrowing fixes it — the arch case is 38 minutes between close and the next commit, which is indistinguishable from a session that marked done and kept working.

This is evidence *for* the sha-per-lane option rather than a reason to keep waiting. A lane carrying the sha that closed it needs no inference about time at all: `merge-base --is-ancestor <sha> main` is a fact about the change the lane claimed, which is the thing `done` asserts. The coordinator verified its six by hand by reading commit messages against lane items — a judgement the audit cannot make and the grammar would not need.

What holds from the attempt: the ceiling of thirty is real, the cost climbs fastest when the fleet is most productive, and the board is the surface Zach reads first. The decision is unchanged and still his; it now has one fewer option and a better reason for the one that remains.

## 117 — a reviewer without the primary evidence catches reasoning, not facts

**From:** the 06:03–06:36 exchange between this lane, `gauntlet-b2` and `board-ux-improvements`. Four false general claims, four corrections, and the corrections were not alike.

Between them: I credited 0.12.5 with a live effect it did not have, then asserted six lanes were not done when their changes were all on main; the coordinator credited 0.12.5 with four reopens its own earlier output attributed to a merge, then asserted a timing invariant its own Log refutes; board-ux told me my inertness claim did not cover its file. Every one was wrong or incomplete, and every one was caught within minutes.

**The corrections sorted by access to the primary artifact, not by care or seniority.** The coordinator and this lane can both read the tracker, and both of our corrections of each other were factual and settled the question on the spot: five shas checked against main, four orphaned lanes enumerated, an A/B run in the same minute. board-ux is barred from the Gauntlet workspace by Zach's constraint and holds to it, so its correction of me could only be methodological — *"identical output proves inertness on the inputs that version touches"* — and it was right, and it was the most useful sentence anyone said in the half hour. But the specific live claim it hung on that reasoning was false: it predicted the board was drawing a session as working against an orphaned lane, and no such lane exists. It could not check, so it reasoned to a confident specific.

That is the finding, and it is a routing rule rather than a criticism of anyone: **a reviewer without read access to the artifact catches errors of reasoning and cannot catch errors of fact, and will supply confident specifics to fill the gap.** Both halves matter. The methodological catch was worth more than either factual one, because it generalises and they did not. The specific was noise that cost two sessions a check.

Three consequences. A verification request goes to a session that can read the evidence — I answered board-ux's orphaned-lane question in one command because I could read the file it cannot, and it had asked the coordinator rather than me. A reviewer held out of an artifact for write-safety reasons is correctly held out and should say *"I cannot check this"* where it currently says *"this is happening"*. And the reasoning-level catch should be solicited deliberately rather than treated as a consolation: board-ux produced it precisely because it could not run the check and had to think about what the check would have proved.

The shape underneath all four errors was the same and the coordinator named it best: reaching for a general statement without searching one's own record for the case that breaks it. Its rule — before asserting an invariant about how sessions behave, search the Log, because if a counterexample exists you probably wrote it down — is the durable half. Mine is narrower and belongs beside it: **before generalising a null result, name which inputs the change actually touches.** I had A/B'd one join and generalised to a neighbouring join in the same file and to a second file, and was wrong about both.
