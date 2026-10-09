# Compliance

Each row is a place where the code disagrees with a section of `ARCHITECTURE.md`. The section stands as written; the row records the gap. Owner is `unassigned` until a slice is dispatched.

| ID | Section | Severity | File and line | Gap | Owner | Status |
|---|---|---|---|---|---|---|
| C1 | 5 | P1 | `evals/test_architecture.py:29` | The test holds the page-module and `DRAWS_NOTHING` lists and checks cycles (`:92`); it holds no circle table and does not fail on an inward-pointing-outward import. | unassigned | open |
| C2 | 6 | P1 | `scripts/dispatch_prompt.py:309` | `compose` never reads a row's `stage`, so a row at `stage: review` gets no `Review:` line telling the worker to run `extras:code-review`. | unassigned | open |
| C3 | 7 | P1 | `scripts/dispatch_prompt.py:309` | `compose` emits no `Architecture:` line, and `scripts/workspace.py` does not parse the `Canonical document:` bullet of a workspace's `## Architecture` block. | unassigned | open |
| C4 | 3 | P2 | `scripts/issue_page.py:20`, `scripts/source_page.py:25` | The page modules import underscore names from `decision_page` and `issue_page`, and the shared page module (`scripts/page_kit.py`) does not exist. | unassigned | open |
| C5 | 4 | P2 | `scripts/audit_tasks.py:24`, `scripts/audit_tasks.py:27`, `scripts/audit_tasks.py:30`, `scripts/task_forge.py:11` | A Use Case imports Interface Adapters and Drivers: `audit_tasks.py` imports `backlog`, `board_sources` and `git_trees`; `task_forge.py` imports `board_sources`. | unassigned | open |
| C6 | 8 | P2 | `scripts/inbox.py:1`, `scripts/audit_tasks.py:1` | `inbox.py` (1285 lines) and `audit_tasks.py` (961 lines) still carry several responsibilities each and are not split. | unassigned | open |
| C7 | 4, 8 | P2 | `scripts/flow.py:196`, `scripts/flow.py:202`, `scripts/flow.py:218`, `scripts/flow.py:276` | `flow.py` (336 lines, merged in #561) holds a Use Case and a Frameworks and Drivers part in one module, and section 4 places it by those parts. It also has three reasons to change: worker reconciliation (`:127`), dispatch (`:231`) and stall detection (`:196`). The stall judgement `check_stagnant_tasks` reads `CLAUDE.md` from disk (`:202`) and runs git through `last_worktree_commit_epoch` (`:218`), so the Use Case does the driver's I/O. | unassigned | open |
