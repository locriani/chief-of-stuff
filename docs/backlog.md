# The backlog lives in GitLab

The tracker reached 158 rows in 22 hours because the ruleset says one row per item, so every finding, defect, question and session report became a row. It had become a second Log with a worse index. Zach, 2026-09-19 22:52: the backlog is tracked in GitLab now that it is no longer broken.

The split: **the tracker holds what is being worked; GitLab holds what is not.**

## Where it is

`labs.gauntletai.com`, project `zachgardner/openemr` (id 1991), issues enabled. That host is the only authorized one for this project — workspace `CLAUDE.md` house rule 2 — and it is also the one the fork already pushes to.

## The config line

In the workspace `## Coordinator` block, the shape `Board:` already uses:

```
- Backlog: GitLab; host https://labs.gauntletai.com; project zachgardner/openemr; token env CHIEF_OF_STUFF_GITLAB_TOKEN
```

`host`, `project`, and the **name** of an environment variable. `parse_backlog` refuses a `token` part that is not `env <NAME>`, and the refusal does not quote what it found — house rule 6 forbids a secret in a committed file, and an error message is a committed file's next stop.

## The token

A personal access token for `zachgardner`, scopes including `api`, **expiring 2026-11-30**. Nothing else will remember that date.

It lives in the macOS Keychain:

```
security find-generic-password -a chief-of-stuff -s gitlab-labs.gauntletai.com -w
```

Resolution order in code is environment → Keychain → absent, and absent is a first-class state that returns `""` rather than raising.

The value is recorded in neither this repo nor a `CREDENTIALS.md`. House rule 6's authorized listings exist so **graders** have working access to the deployed application; this token is Zach's own GitLab credential with write scope on his account, and a file written for Gauntlet is the wrong place for it. The Keychain holds the value; this page holds the coordinates and the expiry, and neither of those is a secret.

## A missing answer is never a clean answer

Finding 116(a)'s rule reaches the client. An absent token, an unreachable host, a 401, a body that is not JSON, and a backlog longer than `MAX_PAGES` all produce `backlog: unknown — <why>` and exit 1. None of them produces a zero, because *nobody could take the count* and *there is nothing in the backlog* are different facts and only one of them means there is no work.

The same rule governs pagination: a truncated read is refused rather than returned, because a short list reads as a short backlog.

## Proved against the live project

- 21 unit tests against a loopback server; the suite never touches the network and never reads the real Keychain.
- Live, clean slate: `backlog: 0 open, 0 closed`, exit 0.
- Wrong project: `backlog: unknown — HTTP 404 from GitLab`, exit 1.
- No token: `backlog: unknown — no token: set $… or add it to the Keychain as gitlab-labs.gauntletai.com`, exit 1.

The negative controls are what make the zero meaningful.
