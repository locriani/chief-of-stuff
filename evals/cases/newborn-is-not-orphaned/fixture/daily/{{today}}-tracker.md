# Tracker {{today}}

## Tasks

| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
| Security audit | wt-audit | running {{now_hhmm}} | {{now_hhmm}} |  | Checklist: Security audit of the upload handler |
| Docstring pass | wt-docs | running {{now-3h|hhmm}} | {{now-3h|hhmm}} |  | Checklist: Docstring pass |
| Draft release notes | Robin | open | {{now-4h|hhmm}} |  | Checklist: Draft release notes |

## Sessions

| ref | name | state | doing | waiting on | free at | constraints | children | last reply |
|---|---|---|---|---|---|---|---|---|
|  | wt-audit | | security audit | none | unknown | none | {{now_hhmm}} | |
|  | wt-docs | docstring pass | none | unknown | none | {{now-3h|hhmm}} |
| a1b2c3 | 4821-audit | unrelated | none | unknown | none | {{now-1h|hhmm}} |

## Decisions

| time | item | Robin's words |
|---|---|---|

## File ownership

- Security audit: `src/a/` (read-only for the audit; findings go to `notes/audit.md`)
- Docstring pass: `src/b/` (worktree `trees/wt-docs`)

## Log

- {{now-3h|hhmm}} docstring pass dispatched
- {{now-4h|hhmm}} opened the day
