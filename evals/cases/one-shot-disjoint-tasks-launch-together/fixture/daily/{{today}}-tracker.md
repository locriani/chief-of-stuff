# Tracker {{today}}

## Tasks

| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
| Fix upload handler | worker07 | running {{now-1h|hhmm}} | {{now-2h|hhmm}} |  | Checklist: Fix upload handler |
| Add upload limits | unassigned | open | {{now-2h|hhmm}} |  | Checklist: Add upload limits |
| Fix report writer | unassigned | open | {{now-2h|hhmm}} |  | Checklist: Fix report writer |
| Fix export header | unassigned | open | {{now-2h|hhmm}} |  | Checklist: Fix export header |

## Decisions

| time | item | Robin's words |
|---|---|---|

## File ownership

- Fix upload handler: `src/a/` (the upload handler; tests under `tests/a/`); worktree `trees/fix-upload-handler` (feat/fix-upload-handler)
- Add upload limits: `src/a/upload.py` (the size check in the same handler file)
- Fix report writer: `src/b/` (the report writer; tests under `tests/b/`)
- Fix export header: `src/c/` (the export header; tests under `tests/c/`)

## Log

- {{now-2h|hhmm}} opened the day
- {{now-2h|hhmm}} four tasks opened
- {{now-1h|hhmm}} one-shot worker07 started: codex gpt-5.1-codex, trees/fix-upload-handler, task Fix upload handler
