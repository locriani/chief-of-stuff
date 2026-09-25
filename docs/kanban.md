# Kanban walk

The tracker's `lane` and `stage` remain the record of a task's progress. A `[kanban]` table in the workspace settings file maps each precise stage to one board column. The `!! - HUMAN REVIEW REQUIRED` label is an additional hold on that column. A coordinator records the decision and tracker transition first, then syncs the corresponding issue. `audit_tasks.py` reports disagreement without changing either side.

For the existing six-column GitLab board, configure the workspace TOML like this:

```toml
[lanes]
designed = { stages = ["design", "design yes", "test", "implement", "pr", "review", "triage", "fix", "verify", "merge", "main"], gates = ["design yes", "triage", "merge"] }
build = { stages = ["plan", "plan yes", "test", "implement", "pr", "review", "triage", "fix", "verify", "merge", "main"], gates = ["plan yes", "triage", "merge"] }

[kanban]
stages = [
  "00 - ARCHITECTURE / PLANNING",
  "01 - ARCHITECTURE / PLAN REVIEW",
  "02 - TEST IMPLEMENTATION",
  "03 - IMPLEMENTATION",
  "04 - CODE REVIEW",
  "05 - REFACTOR / ADDRESS ISSUES",
]
human_review_label = "!! - HUMAN REVIEW REQUIRED"
hold_stages = ["design yes", "plan yes", "triage", "merge"]
terminal_stages = ["main"]

[kanban.map]
design = 0
plan = 0
"design yes" = 1
"plan yes" = 1
test = 2
implement = 3
pr = 4
review = 4
triage = 4
fix = 5
verify = 4
merge = 4
```

New code tasks pass through planning, plan approval, test implementation, implementation, and code review. A review that needs fixes moves to `05` and returns to `04` for verification. The `!!` hold remains until the user's decision. Existing tasks already in a later stage stay there; the migration does not pretend they passed earlier columns.

For a GitHub backlog, omit `[kanban.github_project]` to use issue labels as the columns. To use a GitHub Project's single-select Status field instead, add:

```toml
[kanban.github_project]
owner = "your-org"
number = 12
field = "Status"
options = ["Planning", "Plan Review", "Testing", "Implementation", "Code Review", "Fixes"]
```

The options correspond to `[kanban].stages` in order. In Project mode, the numbered stage lives in Status and `!!` remains an issue label. The updater adds the issue to the configured Project if it is absent. These labels and Status options must already exist; the updater does not create a misspelled one.

Preview all active tracker issues:

```sh
python3 scripts/kanban.py --root /path/to/workspace --date YYYY-MM-DD
```

Sync a single transition after recording the new tracker stage:

```sh
python3 scripts/kanban.py --root /path/to/workspace --date YYYY-MM-DD --issue '#42' --from-stage implement --commit
```

For an issue that has no stage yet, preview it and replace `--from-stage` with `--initialize`. A commit always names one issue. A completed task is excluded from the bulk preview, but can be selected by `--issue` to clear its final board stage. If a board stage changed after the coordinator last observed it, the updater refuses the write and the coordinator reconciles it with the user. Existing `kind::`, `severity::`, and other unrelated labels survive every move.
