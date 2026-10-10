# chief-of-stuff 1.0: design

Status: draft for Zach's review. Written 2026-10-09 from a brainstorming conversation, a read of the repo at origin/main v0.128.64 (PR 604 bumps it to 0.128.65), and a mining of 45 coordinator sessions (2026-09-17 to 2026-10-09, 901 user turns; counts approximate).

Notation: **Decided** is Zach's call, in his words or by his pick. **Call** is a choice made here without asking; Zach should check these. **Open** needs Zach.

## Intent

chief-of-stuff runs Zach's day: it turns issues into dispatched work, drives review and merge, and keeps a board of it. Today it needs constant prodding. 1.0 exists so that it is autonomous by default and Zach is consulted only on exceptional cases.

## What hurts today

From the session mining. Counts are approximate.

| Pain | Count | What happened |
|---|---|---|
| Silent or empty ticks | 127 of 901 turns follow "Mailbox empty" or "." | Zach asks for status or types "continue" |
| Ready work not pushed | ~12 | Idle worker slots with dispatchable work (approved 12 workers, ran 5) |
| Approved work not merged, reviews not launched | ~13 | Stages wait for Zach's cue; reviews launched one at a time |
| Questions it should not ask | ~90, ~10 rebukes | Relayed worker "allow?" prompts, "should I edit CLAUDE.md?", handing work to idle sessions |
| Decision pages wrong | ~30 | Answered but still pending; missing; trivial items that should have been automatic fixes |
| Lost place | ~90 | Restarts, compactions, quota hits and outages; it stopped until Zach typed "continue" |
| Stale or duplicated board | ~14, plus Zach's own report | One issue became 30-40 gantt lines |
| Acted on wrong state | ~8 | Called failing MRs mergeable; said a launch succeeded when it failed |

Pattern (inference, not verified): the coordinator leans on the model to notice what to do. Part of an engine already exists: `flow.py` `tick()` (`scripts/flow.py:281`) dispatches ready tasks and flags stalled ones, `architecture/ARCHITECTURE.md` §9 describes automatic dispatch, and the README describes a Python watcher that audits every five minutes for Codex, Cursor and Antigravity. But the mining shows ticks that said nothing about overall state, slots left idle, and approved work left unmerged, so the part that exists does not cover merge, review launch, resume after quota or outage, or the decision to ask. State is a markdown file parsed by roughly 150-170 regex calls, with older Tasks table layouts still supported (`scripts/tracker.py`), and the identity of a task is a free-text name, so nothing stops a duplicate. Each past fix was added as prompt text or memory with no gate in the engine.

## Goals

1. **Autonomous by default.** No routine nudges. Success signs, all four picked by Zach: zero routine nudges; one gantt line per issue; merges land unprompted; survives a restart with no catch-up prompting.
2. **One gantt line per forge issue.** Everything done for an issue (dispatch, review, rework, merge) appears as segments on that one line. Duplicates cannot be created.
3. **Survives restarts, compaction, quota and outages** and resumes on its own.

## Non-goals (Decided)

- Migrating old daily logs. Past trackers stay as archived files; the new store starts fresh and does not import the five legacy layouts.
- Multi-user or team use.
- New agent types or features. 1.0 reproduces current behaviour plus the goals above.

Not a non-goal: the board UI may be adjusted where the new data model needs it (see Board).

## Anti-goals

A 1.0 that works but still needs prodding along has failed (Decided). So has a 1.0 where the same narrow bug-fix PRs keep arriving in the same areas (the current churn is concentrated in `audit_tasks.py`, `one_shot.py`, `dispatch_prompt.py`, `git_trees.py`).

## Decisions

1. **State moves out of markdown into a structured store** (Decided). Zach never opens the tracker or daily log; only agents do. Tasks, decisions, file ownership, log and resume state live in the store. Markdown survives only where something needs a readable export. The store is accessed through an ORM (Zach's standing rule: ORMs are required).
2. **The issue is the identity** (Decided). One forge issue is one gantt line and the primary key of its work. Agents write by upsert on that key, so a second line for the same issue is impossible, not merely discouraged.
3. **No work without an issue** (Decided). If work starts with no forge issue, the coordinator creates the issue first.
4. **The engine drives; the model advises** (Decided). A long-running deterministic engine reconciles state against git and the forge on its own schedule: dispatches ready work into free slots, launches reviews, merges when policy is met, resumes after quota or outage, re-renders the board. It calls a model only for judgement (reviewing, real ambiguity). The engine, not the model, decides whether to ask Zach.
5. **The review-and-merge route is a declarative policy the engine runs** (Decided). The route as Zach describes it: a trio of reviewers, burn down to no open findings, consult the architect. The model does the reviewing; the engine enforces the gates and merges when the policy is satisfied. Merging under the policy is not an interrupt. Note: this route is not written down in the repo or in the Gauntlet workspace config. The repo has a single `reviewer_session` or an `extras:code-review` worker plus an optional `architecture_reviewer` that receives only filed findings (`skills/review-pipeline/SKILL.md`, `architecture/ARCHITECTURE.md` §6). Its concrete content is Open.
6. **Exceptions that interrupt Zach** (Decided): real ambiguity in the goal that nothing in the repo, issue or earlier decisions settles; a failure the coordinator cannot recover from after its own retries; budget or quota about to exceed a limit he set. Everything else it decides alone. Merging under the policy is explicitly not on this list. This overturns the default `[workflow] merge_owner = "user"` and the human-only-actions section of `agents/chief-of-stuff.md` (lines 119-120, 146); the Gauntlet workspace currently runs `merge_owner = "worker"` as a temporary waiver from 2026-10-02.
7. **All four hosts stay** (Decided): Claude, Codex, Cursor, Antigravity. The core is host-neutral and each host gets a thin adapter. Call: Codex already has a session-scoped Python watcher (README); its real gap is that it cannot wake an idle conversation. With the engine owning scheduling and waking, that gap stops being the host's problem.
8. **Board** (Decided): adjust it where the model needs it; no redesign. Call: rework and review show as segments on the issue's single line.

## Constraints

- Any new library, package or external tool needs Zach's approval before it is installed, vendored or depended on: the ORM, the store, anything else. Name it, say where it comes from and why, and wait for his yes. Researching candidates is fine.
- The existing test suite is the main safety net (about 49k lines of tests, 110 eval cases). Whatever the build approach, the behaviour it encodes must be carried over or consciously dropped.
- The plugin repo protects `main`; every change lands through a PR and bumps the version.

## Left to the builder

- The concrete store, schema and ORM (candidates to be presented to Zach for approval).
- The engine's process model: how it is started, supervised and restarted, and how it talks to each host's session.
- How the model-facing prompt shrinks once the engine owns the loop.
- The policy file format for the review route.
- Which current behaviours (notifications, quota relaunch, worktree hygiene, resource cleanup) carry over unchanged. Stale worktrees used 185 GB once, so cleanup is a behaviour to preserve.

## Open

- **The review policy's content**: reviewer roles and models, what counts as an "open finding", retry limits, when the architect is consulted, and whether the existing Triage rule (automatic fix versus ask; security always asked, `skills/review-pipeline/SKILL.md`) survives as the engine's rule for a finding that is neither a clear fix nor ambiguous.
- **Whether the Clean Architecture contract** (`architecture/ARCHITECTURE.md`, enforced by `evals/test_architecture.py`, every script placed in a circle) still binds new code, and whether the open compliance rows C1-C8 are in scope.
- **Which current behaviours carry over versus are dropped**: tick, kanban, notify, PARA filing, the herdr/tmux/ghostty launchers, `claude_profile`, the mailbox, decision pages.
- **Budget and quota limits**: where Zach sets the limit that triggers an interrupt. No repo setting for it was found.
- **Gantt segments**: what a segment is keyed on when rework yields several PRs or workers for one issue. Call: key segments by (issue, stage, attempt).
- **Forge coverage for "create the issue first"**: GitHub and GitLab, and workspaces with no `Backlog:` line. Call: a workspace with no backlog gets no engine-created issues and the engine says so once.

- **Build approach** (parked by Zach): new core with the old tests as spec, strangler replacement, or clean-slate. Decide before the plan.
- **What the engine does while a host session is closed**, and which host sessions are persistent coordinators versus launched on demand.
- **Whether Zach wants a persistent coordinator session to talk to at all** or only the engine, the board and exception pings.
