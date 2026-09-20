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

## Writing

`create`, `close`, `comment`. **`--dry-run` is the default and `--commit` is required**, because a write is outward-facing and the GitLab *web UI* is on the coordinator's human-only list. The API is not the web UI, but the caution transfers: the default has to be the mode that cannot do damage. A run without `--commit` prints every write it would make and says on stderr that nothing was written.

There is deliberately **no `--token` flag**. `argv` is readable by `ps` and lands in shell history; the token comes from the environment or the Keychain and from nowhere else.

Labels are created on demand from whatever a write asks for. **No vocabulary is baked in** — Zach, 2026-09-19 22:47: *"lanes are also going to be changeable over time."* Hardcoding the seven workstream names would have frozen a list he had already said would move, and deriving labels from lane prose would repeat finding 121, where the prose is the join key. The caller names the labels; `ensure_labels` reads the project's labels and creates only the ones GitLab has not seen. A `create` makes its labels *before* the issue, because GitLab drops an unknown label rather than refusing the request.

A refused write never borrows a success's wording: `create failed: … — HTTP 403 from GitLab`, and `done` is the only field that means it happened.

## Proved against the live project

- 21 unit tests against a loopback server; the suite never touches the network and never reads the real Keychain.
- Live, clean slate: `backlog: 0 open, 0 closed`, exit 0.
- Wrong project: `backlog: unknown — HTTP 404 from GitLab`, exit 1.
- No token: `backlog: unknown — no token: set $… or add it to the Keychain as gitlab-labs.gauntletai.com`, exit 1.

The negative controls are what make the zero meaningful.

Write side, one real round trip on project 1991 — a fixture cannot prove the header, the verb and the label creation all land the way GitLab expects:

```
would create: Backlog client round trip        # the default, and it wrote nothing
created #1: Backlog client round trip          # with --commit; `tooling` created on demand
backlog: 1 open, 0 closed
#1 Backlog client round trip [tooling]
commented on #1
closed #1
backlog: 0 open, 1 closed
```

Issue #1 stays closed on the project rather than being deleted. It is the record that the client works.

## Migrating what is still live

`scripts/migrate_backlog.py`. Two steps, and a human between them.

```
python3 migrate_backlog.py --tracker <tracker.md> --report <file.md>     # proposes; creates nothing
…Zach edits the disposition column…
python3 migrate_backlog.py --tracker <tracker.md> --apply <file.md> --commit
```

**Nothing here decides.** The coordinator measured the rows' freshness at 04:00 and found several that had closed themselves during the night and at least two that were wrong about their own state. A confident disposition would have carried those into GitLab as written. So every row gets a proposed word and the reason it was proposed, and Zach corrects the column.

Four words: `move` (create it, drop the tracker row), `keep` (it is being worked), `closed` (already finished; the tracker keeps the record), `check` (the row disputes itself).

`check` fires when the state cell says open or waiting while the row's own prose **shouts** a completion — `**FINISHED 00:12**`, `CLEARED 01:05`, `DELIVERED 00:02`. The match is case-sensitive on purpose: matching case-insensitively flagged seventeen rows, most of them for the word "resolved" in an ordinary sentence. It is still deliberately over-inclusive — one of the three it catches on the live tracker turns out to be a row whose *blocker* was cleared, not the row. That is the right trade: `check` costs one human glance, and the alternative is a wrong row in GitLab.

**Two hashes, because the tracker moves while the report is being read.** Each row is keyed by a hash of its own text, so a row edited after the report gets a new key and is skipped rather than migrated as something else — *no disposition is not consent*. And the report records a fingerprint of the whole tracker, so the apply step can say the triage is stale instead of quietly working from it.

The table is read through `render_board`'s splitter, which is mark-aware — a `|` inside a code span is a pipe, not a cell edge (finding 100). Writing a second splitter here would have reintroduced that bug; `short_name` and `clip_name` are borrowed the same way, and for the same reason.

First run on the live tracker, 165 rows: **82 move, 34 keep, 46 closed, 3 check.**
