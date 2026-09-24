# Workspace

## Coordinator

- User: Robin
- Daily log dir: `daily/`
- Daily log template: none
- Tracker: `daily/<date>-tracker.md` (sections: Tasks, Decisions, File ownership, Log)
- Worktrees: `trees/`
- Health: agent {{health_base}}/ready 200
- Health: login {{health_base}}/login 200
- Timezone: {{tz}}
- Calendars: none
- Deadlines: none
- Sessions: list `mcp__peers__list_sessions`; send `mcp__peers__send`
- Board: `mcp__board__publish`; URL board-known
- Human-only actions: dashboard or console changes; `railway` commands; git commit, add, or push; spending money
