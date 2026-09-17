# Design inputs

Field findings weighed at replans. Status per finding is in the table; the plan (`/Users/locriani/.claude/plans/it-s-the-ramp-card-crystalline-cupcake.md`) governs.

## 2026-09-16 — a coordinator session dispatching to peer sessions on deadline night

Relayed from the coordinator session, at the user's request. Workspace names and session names are removed.

| # | Finding | Candidate rule | Touches | Status (rev 3) |
|---|---|---|---|---|
| 1 | Bundling an assignment with a status request produced three declines out of four. Sessions had standing instructions the coordinator could not see, and one already had the same task from the user. Each decline cost the user attention. | Poll before dispatch, in a fixed 3-line format: current task · state · standing constraints from the user, and whether waiting on the user. Then show the user a table of unowned items. The user creates or assigns sessions. | Dispatch move; new "poll" move | Parked: stage 5 |
| 2 | A session renamed itself mid-evening. Sending to the old name failed with "no agent reachable". | Keep a registry keyed on the socket or session id, with the name as a label only. Re-list on send failure. | Peer registry | Parked: stage 5 |
| 3 | The user approved a session's plan minutes before the coordinator assigned the same item. The coordinator had no view of that approval. | Keep a ledger of the user's own approvals (session, item, time) and check it before any dispatch. The user's assignment always outranks the coordinator's. | Dispatch precondition | Adopted: tracker Decisions table; case 15 |
| 4 | Standing constraints live in sessions, not files. Examples: a directory off-limits, no commits without explicit approval, per-step approval, infra changes need a go. | Each session declares its constraints at registration; the coordinator stores them. | Peer registry | Parked: stage 5 |
| 5 | A "commit locally" instruction violated one session's standing rule. | Default policy: write only, no commit. Commits are batched after the user's review. | Dispatch prompt template | Adopted: dispatch prompt template; case 11 |
| 6 | A research plan file plus a tracker file on disk, sent as paths, gave sessions with no shared context enough to act coherently. | Keep one tracker file holding owners, due times, a decision table, a file-ownership list (no cross-edits), and an append-only log. | Relates to the daily log + lanes design | Adopted: separate per-day tracker file |
| 7 | Idle notices arrived stale and out of order: notices stamped 16:54–16:58 were delivered after 17:16. | Do not derive state from idle notices. Direct messages from sessions are the signal. | Notification move | Parked: stage 5 |
| 8 | The coordinator's own subagents never collide with the user's sessions, but only for work the user handed the coordinator directly. | Subagent dispatch is limited to items the user assigned to the coordinator. | Dispatch move (already the plan's only channel) | Adopted: dispatch only on the user's yes |
| 9 | The first line of a message is its preview. The fixed 3-line status format got clean answers from every session. | The first line of every outgoing message states the whole ask in one sentence. | Message template | Adopted: asks and dispatch prompts; case 11 |

## 2026-09-16, later — same coordinator session

| # | Finding | Candidate rule | Touches | Status |
|---|---|---|---|---|
| 10 | The coordinator estimated elapsed time instead of reading the clock. It logged 17:12–17:55 while the real clock ran 16:58–17:09, about 45 minutes fast, and overstated schedule pressure to the user. | Read the clock for every log or tracker entry that carries a time; never estimate elapsed time. | Clock rule; tracker Log | Proposed: extends the rev 3 Clock rule (which covers stated times) to written timestamps. A timestamp-tolerance grader on cases 8 and 12 would test it. Not adopted until a replan |
| 11 | Peer idle notices carry the real finish time. They arrive late (finding 7), but the time stamped in them is correct, so they are the cheapest clock check available. | Use an idle notice's stamped time as a clock reference, never as current state. | Notification move; refines 7 | Parked: stage 5 |
| 12 | A target session renamed a second time in one evening; the bare name stopped resolving between two messages. | Confirms 2: key the registry on the session ref, never the name. | Peer registry | Parked: stage 5 |
| 13 | The user now requires session names prefixed with the process id (`<pid>-<work>`) so identity survives renames. A session cannot rename itself without a tool; the user runs the rename. | Registry key is the pid (from the socket path); the rest of the name is a label. | Peer registry; refines 2 and 12 | Parked: stage 5 |
| 14 | A session cannot rename itself; `/rename` is terminal-only, so the user typed it in seven terminals. The coordinator's own rename invalidated the reply address every peer held and forced a broadcast. | The harness names sessions at spawn and never renames a live address. | Peer registry; session launch | Parked: stage 5 |
| 15 | The user now requires every agent given a new task to plan it first: produce the plan, present it for the user's approval, write nothing until approved. A build that skipped planning was reverted the same evening. | Default dispatch shape is plan → approval → execute, with the plan gate enforced by the harness (the dispatched context starts in plan mode), not by the assignment text. | Dispatch move; dispatch prompt template; a new "plan review" move | Proposed for the next replan. Rev 3's dispatch is propose → yes → launch; this adds a second gate after launch. Case 12/13 graders would change |
| 16 | The user's rule (20:27): all work happens in git worktrees, one per task on its own branch; nothing is written in the main checkout. With 15 (plan first) and 5 (no commits by sessions; the user reviews and commits) this gives a natural unit of work. | A task = worktree + branch + plan + review. The dispatch prompt's `Owns:` line names a worktree and branch, not paths in the main checkout; the harness creates the worktree before launch and the tracker records it. | Dispatch prompt template; Tracker (File ownership becomes worktree ownership); a "review" move | Proposed for the next replan, with 15 |

## Replan 2026-09-16 (rev 3): the user's answers

- Coordinate other live sessions? **Later stage.** Peer coordination is stage 5, after live use; it gets its own replan.
- Subagents for work? "I meant that it could launch new contexts with my approval (e.g. new claude instances)." Work dispatch on an explicit yes stays. A new Claude instance is a peer, so that channel lands in stage 5.
- Tracker location? **Separate tracker file**, per day, next to the log.
