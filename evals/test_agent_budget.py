"""A byte budget for agents/chief-of-stuff.md and the prompts rendered from it (#407).

The agent file is loaded whole at every session start, so its size is a recurring cost. These are
ceilings, not targets.
"""

import hashlib
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import runtimes  # noqa: E402
import start_coordinator as start  # noqa: E402
from evals.rules_text import one_shot_paragraph, rules_text, sections, skill_body  # noqa: E402
from evals.skill_fixtures import write_plugin, write_skill  # noqa: E402

# Each later slice lowers these in a red commit before it trims the text.
# Slice 7 (#490): simulated verbatim move from origin/main, retaining only
# processes, listing-is-not-roster and orphan paragraphs in Sessions.
# Agent 54,973 B; Assign 283 B; Sessions 1,663 B. Round ceilings up to 50 B.
TOTAL = 55_000            # whole agent file, bytes
MAX_LINE = 8_400          # longest single line
ONE_SHOT_PARAGRAPH = 4_400  # kept: a later slice trims this paragraph
# Skills are read on demand; no host's prompt appends their bodies.
# rendered_sizes(1) over the intended agent in a temporary generic plugin:
# Claude 54,621 B; Codex 52,628 B; Cursor 52,736 B; AGY 52,792 B.
# Paths removed; each ceiling rounds up to 50 B. Skills remain on demand.
PROMPT = {"claude": 54_650, "codex": 52_650, "cursor": 52_750, "agy": 52_800}
CEILING = {  # `## ` section -> bytes, heading line included
    "Role": 1600, "Dispatch authority": 1100, "Browser safety": 410, "Writing": 780,
    "Config": 1400, "Clock": 1850, "Calendar": 910, "Open the day": 2000, "Resume": 4830,
    "Write authority": 2310, "Filing": 360, "Human-only actions": 1280, "Dispatch": 15210,
    "Assign": 300, "Pipeline": 1_300, "Check": 2780,
    "Sessions": 1_700, "Relay": 2510, "Tracker": 5_500, "Notices": 740, "Board": 1750,
    "Requirements": 360, "Share": 460, "Asks": 4700, "Notify": 360,
}
# coordinator-sessions: 8,587 B moved prose; projected frontmatter/topics yield
# 8,875 B, leaving 425 B headroom under its 9,300 B ceiling.
SKILL_CEILING = {"decision-page": 2_700, "optional-features": 5_200, "tracker-rows": 5_400, "review-pipeline": 9_677, "coordinator-sessions": 9_300}  # moved bytes + metadata/organization/headroom
SKILLS_TOTAL = 32_277  # all SKILL.md files, bytes; slice 7 adds the same 9,300 B ceiling
DESCRIPTION_CAP = 300  # frontmatter description characters, not bytes
SKILL_POINTER = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/skills/([^/\s`]+)/SKILL\.md")
DECISION_PAGE_POINTER = "Before writing a decision page, Read ${CLAUDE_PLUGIN_ROOT}/skills/decision-page/SKILL.md"
OPTIONAL_FEATURE_POINTERS = {
    "Filing": "Before moving a file into a PARA home, Read ${CLAUDE_PLUGIN_ROOT}/skills/optional-features/SKILL.md",
    "Requirements": "Before ticking or editing a requirements file, and when a task goes done and the Coordinator block names one, Read ${CLAUDE_PLUGIN_ROOT}/skills/optional-features/SKILL.md",
    "Notify": "When the Coordinator block has a Settings line, Read ${CLAUDE_PLUGIN_ROOT}/skills/optional-features/SKILL.md",
}
OPTIONAL_FEATURE_RULES = {
    "Filing": (
        "A move is `mv`, nothing else.",
        "Never copy, delete, rename, or edit what you move.",
        "Never move anything outside the workspace root unless the user has asked for it.",
    ),
    "Requirements": (
        "Your only edit is a tick: flip `- [ ]` to `- [x]` and append ` — evidence: <commit, URL, or Log HH:MM>`, with Edit.",
        "Never add, remove, reorder, or reword an item, never untick one, and never tick without evidence.",
        "A task that matches no item ticks nothing.",
    ),
    "Notify": (
        "Notifications are off by default, including an empty `[notify]` table.",
        "Never run routine `notify sync` or edit the queue yourself.",
        "Once per ask, in the same move as the ask.",
    ),
}

TRACKER_ROWS_POINTER = "Before writing or changing a Tasks row, filing or closing its issue, or moving its stage, Read ${CLAUDE_PLUGIN_ROOT}/skills/tracker-rows/SKILL.md"
# Verbatim sentences from the Tasks bullet, pinned before slice 4 moves them.
TRACKER_ROW_RULES = {
    'pipe and name': (
        'A `|` inside any cell is written `\\|`, backticks or not: a raw one is a column delimiter, and the board reads every column right of it one place over.',
        '`name` is what the task is called: a short noun phrase, yours to write when you write the task and to rewrite when the item changes, and never more than a line.',
        'It is the label the board draws for a task; `item` keeps the wording, the history you append, and the status note, and is read only when someone opens the task.',
        'Write the name because you know which words are the task and which are its history — the board cannot tell, and before this column it guessed by splitting the item cell on its first sentence, which is why tasks drew as `**Deploy main.**…` with the asterisks showing.',
        'A task whose row has no name still draws: the board falls back to that guess, so an older tracker keeps working and is not to be rewritten wholesale to add the column.',
    ),
    'sha': (
        "The optional `<sha>` is the commit that carried THIS task's change onto main — the one you already reported in `Verified`, written here so a machine can read it.",
        "`audit_tasks.py` clears a task from `git merge-base --is-ancestor <sha> main` when it is there, and falls back to asking whether the owner's TREE is on main when it is not; a sha it cannot parse or cannot find is treated as absent, never as a clear.",
        'Write it only when you have it, and never one you did not verify: a task reopened in error is noisy and self-clearing, and a task cleared in error is silent and permanent.',
    ),
    'size': (
        '`size` is your judgement of the item as written, `S`, `M`, `L` or `XL`, made when you write the task and remade when the item changes: `S` is one edit, one file, one fact to check; `M` is one item a session finishes in a sitting, a few files, one suite run; `L` is a plan with bullets, several files, its own pull request; `XL` is a task that spawns other tasks or spans sessions.',
        'It is not a guess at hours — the board measures those from closed tasks of each size — and a value a person wrote stands.',
        'A size arrives when you write the row or its item changes, never in a move about another task: a tracker you inherit with six columns is widened, with every size blank, at Open the day, and until then it is read as it is — the board parses each width.',
    ),
    'due, issue, lane and stage': (
        "`since` and `due` are what the board draws from: `due` is `HH:MM`, a date, a deadline name from the block, or blank: a blank `due` leaves the board to derive an end from the owner's queue and draw the remaining stages as forecast; it stays blank until a person fills it, and you never fill it in to make the board look complete.",
        '`issue` is the issue that backs the task, when the `## Coordinator` block has a `Backlog:` line: `#N` in that backlog, `owner/repo#N` on GitHub, or the issue\'s URL (GitHub or GitLab) (the user, 2026-09-22 22:20: "each entry in the task tracker is actually backed by an entry in github").',
        'A workflow step — a review, or anything the pipeline itself performs — takes no issue: write `workflow` in its `issue` cell.',
        "A review you are asked to do is a workflow step with its own `workflow` row; a review stage on a task is a change of state on that task's own issue.",
        'Otherwise file it **before** you write the row.',
        'On GitLab (the user, 2026-09-23 22:40: "make everything use gitlab now that we have that going"): `chief-of-stuff backlog --create <the name> --body <outcome, why, acceptance> --commit`, which prints the new number.',
        'On GitHub: `chief-of-stuff backlog --create <the name> --body <outcome, why, acceptance> --commit`; use `--parent <issue-number>` or repeated `--blocked-by <issue-number>` for native relationships rather than writing issue numbers in prose.',
        'The body goes to `gh` on stdin, and the created issue number is printed.',
        'Writing `done` on a task closes its issue in the same move: `chief-of-stuff backlog --close N --commit`, and one Log line.',
        "A standing session's placeholder is not a task and takes no issue, and neither does a row that went `done` before the rule.",
        'A change of state — a kanban stage, a hold label, a review or merge step — is recorded on the issue it concerns, in the stage column and the Log, never as a new issue.',
        '`lane` names a lane from the `[lanes]` table of the block\'s `Settings:` file, and `stage` is where the task stands in it (the user, 2026-09-23: "A lane: the sequence that a task has to move through").',
        'A lane is its stages in order; a gate is a stage only the user\'s word passes, and the board marks a task at one "waiting on you".',
        'A task with no tree (a decision, a relay, a console action) leaves both cells blank and draws no card in BUILD.',
        'Move `stage` with `chief-of-stuff log --root . --stage "<name>" <stage>` when the task moves, once for every stage it enters, even two in one move: it sets the cell and writes the `stage: <name> → <stage>` Log line the board\'s Flow charts read, under the tracker lock.',
        'Never set the cell by hand or write that line as free text.',
    ),
}
TRACKER_RETAINED_RULES = (
    '- **Tasks**: one row per item: `| name | item | owner | state | since | due | size | lane | stage | issue | checklist |`.',
    "Owner is the user's name, a working session, a dispatched context, or `unassigned`.",
    'State is one of `open`, `running HH:MM`, `waiting`, `orphaned`, `done`, `done HH:MM`, `done HH:MM–HH:MM`, `done HH:MM–HH:MM <sha>`; no other words.',
    '`done` keeps the running start — `running 21:16` closes as `done 21:16–22:05`.',
    'The start is the one duration the tracker ever records, and `done 22:05` alone throws it away.',
    '`unassigned` is not the user: an item is theirs only when they took it, and only an `unassigned` task may be proposed for dispatch or assignment.',
    "A task is `done` only when its change is integrated into main **and the suite has been run on main after integration**: work sitting on a branch, in a worktree, in an open pull request, or uncommitted is `waiting`, however green its suites, and merged-but-untested-on-main is `waiting` too — except a task closed because its issue closed on the forge, `done` at the close time regardless, since the forge closure is someone's decision that the task is over.",
    "An open pull request's URL goes in the item when `[workflow] delivery` is `pull-request`.",
    "The configured delivery and merge owner govern how code reaches main, and the reason the suite runs on main after it is that a merge can break main without either side's branch suite noticing.",
    'No script can watch a suite run, so the evidence is a claim with a citation: the owner reports the suite result and the commit main was at, and that sha goes in `Verified`.',
    "`audit_tasks.py` prints main's current sha on every run — when it no longer matches the one in `Verified`, the evidence is about a main that no longer exists and the task is not closed on it.",
    'Before writing `done` on a task whose File ownership names a worktree, run `chief-of-stuff audit --date <today>`, check `chief-of-stuff inbox list --recipient coordinator --unread` for any pending worker reports or stops on that task, and believe the audit and verified evidence over any unverified claim.',
    'Tasks with no tree — a decision, a relay, a deletion, a console action — are unaffected: there is nothing to merge, and the rule never sends you looking.',
    "A task whose File ownership names no worktree closes on its owner's or its reporter's word; going through the workspace for a file a session says it wrote is checking their work, which is not yours to do.",
    'When that word names a sha as on main, check it first: `chief-of-stuff audit --sha <sha>` fetches and prints whether origin/main or local main holds it, and a sha it finds on neither closes nothing.',
    'Main not yet pushed is a `Verified` fact, not a state.',
    "A task going to `done` ticks its checklist item in today's log.",
    'A tracker with eight columns is widened to nine at Open the day, the `issue` cell blank until it is filed.',
    'A tracker with nine columns is widened to eleven at Open the day, both cells blank.',
)
TRACKER_DELETED_RULES = (
    "`audit_tasks.py` reads every issue's state and reports an open task with no issue, a cell that is not an issue or `workflow`, an issue that is closed or missing under an open task, and an issue still open under a done one; its `issue: unknown` line is a check that did not run, never a pass.",
    "`audit_tasks.py` reports a lane the settings file does not name, a stage that is not one of its lane's, a lane with no stage, and a stage with no lane.",
)


REVIEW_PIPELINE_POINTER = "Before moving a task's stage, sending its pull request to the reviewer, handling a Reviewer pass report or an over budget line, running the kanban command, checking review state, or merging, Read ${CLAUDE_PLUGIN_ROOT}/skills/review-pipeline/SKILL.md"
REVIEW_GATE_BULLET = "- A gate yes covers every stage up to the next gate in that task's lane. For a lane that starts past its first gate, as `build` does, interactive dispatch approval, or one-shot Dispatch authority for a ready task, is the entry authorization. A gate passes only on the user's word in chat, quoted in a Decisions row; the one exception is the `triage` gate, which a Triage pass that leaves nothing for the user passes by the automatic-fix rule, recorded in the Log."
REVIEW_NO_CLI_MERGE = 'Never merge one it lists as blocked, never merge or approve with `gh` or `glab` directly, and name what blocks the rest.'
# Minimal adjacent sentences from origin/main's Pipeline: keep the listing that
# supplies the referent of "it" in the blocked-request guardrail.
REVIEW_DEFAULT_MERGE = "Follow `[workflow] merge_owner`: the user merges by default; with `approval`, the user's approval on the platform is the yes."
REVIEW_APPROVAL_MERGE = 'Run `chief-of-stuff merge-approved --root .` whenever you check review state, and merge each `ready` one with `--merge N`, in merge order.'
REVIEW_MERGE_WORDING = 'Never write "mergeable" or "ready to merge" for a pull or merge request except from the `ready` verdict of `chief-of-stuff merge-ready`, and quote its pipeline id and sha.'
REVIEW_RETAINED_MERGE = (REVIEW_DEFAULT_MERGE, REVIEW_APPROVAL_MERGE, REVIEW_NO_CLI_MERGE, REVIEW_MERGE_WORDING)
# Verbatim sentence pins captured before slice 6; never derive these from the
# current agent/skill at test time, or a deletion would delete its own test.
REVIEW_PIPELINE_RULES = {
    'Pipeline': (
        "A task with a `lane` moves through that lane's stages (see Tracker).",
        'In one-shot mode, represent review and follow-up fix work as scoped Tasks rows and launch fresh one-shot workers; do not send assignments to persistent reviewer or implementer sessions.',
        "Include the PR, findings and recorded dispositions in each task; an automatic fix's task names its Log line instead of a disposition.",
        'Review findings still go to the user for disposition, except an automatic fix under Triage; configured lane gates and human-review holds still apply.',
        '`[workflow] delivery` selects `pull-request` (the backward-compatible default) or `branch`; `[workflow] merge_owner` selects `user` (default), `worker` or `approval` for pull requests.',
        'When `[workflow] reviewer_session` names a session, send pull requests to it for review and bring its findings to the user for triage.',
        'With no configured reviewer, ask the user how to review a pull request before advancing its review stage.',
        'A branch delivery reports its branch and head sha; do not apply pull-request review steps to it.',
        "When the Settings TOML has `[kanban]`, its stage map projects the tracker's precise stage onto the visible board label configured in `stages`.",
        'The configured `human_review_label` is an additional hold on that stage, never a replacement for it.',
        "The configured hold stages wait for the user's decision; a worker's report alone cannot clear the hold.",
        'After recording a permitted stage transition in the tracker, sync its one issue with `chief-of-stuff kanban --root <workspace> --date <today> --issue <issue> --from-stage "<previous stage>" --commit`.',
        'For a newly filed issue with no board stage, preview first and use `--initialize` in place of `--from-stage`.',
        'The updater works with GitLab labels, GitHub issue labels, or a configured GitHub Project Status field and issue hold label.',
        'It preserves unrelated labels.',
        'If it reports a manual board move, missing label or API error, leave the tracker stage as recorded, report the drift and reconcile it with the user; do not force the update.',
        'An audit reports drift but never moves cards.',
        'Note a `workflow` row has no issue and takes no sync.',
        'Advancing on a covering yes: move `stage` with `chief-of-stuff log --stage` (see Tracker) and write a Decisions row citing the yes, except for a Triage pass that leaves nothing for the user, whose Log line is the record.',
        'Never advance into a human-only action, a file another live session owns, an orphaned task, or a stage the yes did not reach; stop and ask instead.',
        "At `pr`, only for pull-request delivery: the owning session reports the pull request's URL and head sha.",
        'Write the URL in the item and set `stage` to `review`.',
        'If `[workflow] reviewer_session` names a session, send it `review PR <url>` with a second line, `Report by: chief-of-stuff inbox --mailbox-dir <workspace root>/.chief-of-stuff/mailbox send --to coordinator --from <reviewer session> --type review --task "<task>"`, with `<workspace root>` written out as an absolute path, so its fallback report lands in the workspace mailbox and not in the repo it reviews (SendMessage; when it is not listed, use `chief-of-stuff inbox --mailbox-dir <workspace root>/.chief-of-stuff/mailbox send --to <reviewer session> --type review --task <task>`).',
        'Its report comes back headed `Reviewer pass:`.',
        'Without a configured reviewer, ask the user how to review it.',
        'At `verify`: once every fix has reported, send the configured reviewer the R-numbers the user marked `fix`, quote the Decisions row that marked them, add the R-numbers of the automatic fixes with their Log line quoted, and add the same `Report by:` line.',
        "The user's `fix` marks cover this and the automatic fixes need no yes; do not ask again.",
        "The reviewer runs one full pass and one verify pass; a third needs the user's word.",
        "The user's review comments on a pull request are work.",
        'Whenever you check review state, run `chief-of-stuff review-threads --root .`.',
        'It lists the unresolved threads `[workflow] approver` wrote in: `needs work` while the user has the last word, `awaiting you` once somebody answered.',
        "Each `needs work` thread is the user's own disposition, so it needs no triage ask: append its link to that pull request's task item as a fix, set `stage` to `fix`, and send it to the owning worker; then `verify`.",
        'Leave an `awaiting you` thread to the user.',
        "Never resolve or answer a thread yourself; the worker answers with its commit, and resolving is the user's.",
        'With no `fix` items left and no ask open, the task sits at `merge`.',
        'With `worker`, the owning worker merges a `ready` request; `chief-of-stuff merge-approved --root .` lists them with their merge-ready lines.',
        'Tell the owning worker to merge it, quoting the line.',
        'After a merge, run the main-branch suite before the task is done.',
        'A human-only line that lists merging outranks the setting.',
        "Budgets: `audit_tasks.py` prints `over budget: <task> running <time> (<size> <budget>)` from the settings file's `[budgets]`.",
        "Poll that task's session and tell the user; never stop or kill it.",
    ),
    'Triage': (
        "On a `Reviewer pass:` report, set `stage` to `triage` and ask the user once: list each finding left for the user by its R-number, each with its axis, severity, what it is, and the reviewer's suggestion, and ask for every disposition in one reply: `fix`, `file`, `keep` or `discard`.",
        "The reviewer's findings comment on the pull request is the page (see Asks), so write no second one.",
        'A finding is an automatic fix only when the reviewer suggests `fix`, its evidence names one concrete change (a defect or a test gap, with no design choice to make), its severity is Important or Minor, it is not on the security axis and does not concern security in substance (a path check, authentication, input handling, secrets, permissions), whatever axis the reviewer gave it, and it is not about behaviour the user specified in a Decisions row, in chat, in a spec or in an acceptance item.',
        'Ask about every other finding, including one that fits neither group: when in doubt, ask.',
        'A Critical finding, a security-axis finding, a design or architecture call, a change to a spec or an acceptance item, a finding about behaviour the user specified, and a finding the reviewer suggests `keep`, `file` or `discard` are always asked.',
        'A finding whose axis label names security among several (for example correctness/security) is always asked.',
        "Before you send an automatic fix, read the task row, the Decisions and the Log for anything about the finding's subject; a finding that touches anything found there is asked, not sent.",
        'Send each automatic fix at once, without asking, to the owning session with `Plan: enter plan mode (EnterPlanMode) for this task before anything else; write nothing until the user approves the plan.`, as every assignment is, and leave it out of the ask.',
        'In one-shot mode an automatic fix is a scoped Tasks row launched as a one-shot worker (see Dispatch) with no `Plan:` line; the Plan line applies only to a session handed the task.',
        "Record an automatic fix in the Log, never in Decisions: one Log line naming its R-numbers and saying it was an automatic fix, not the user's word, and list those R-numbers in one line in your next status.",
        'Keep `stage` at `triage` while any ask is open, and move it to `fix` when nothing remains for the user.',
        'When no finding remains for the user, make no ask.',
        'A Triage pass that leaves nothing for the user passes the `triage` gate by this rule, and its Log line is the record.',
        "The ask is the reply's last line and carries that link, or the pull request's when the pass names no comment: `Fix, file, keep or discard for R1–R3 (https://github.com/o/app/pull/7)?`",
        "One verify pass covers the automatic fixes and the user's `fix` items together, once every fix has reported.",
        "An automatic fix a verify pass reports unresolved is put to the user, and a verify pass's own new findings are never automatic.",
        'The task does not reach `merge` while an ask is open.',
        'A verify pass puts only what is still open to the user: a `fix` finding it reports resolved needs no disposition.',
        'A verify pass with nothing open moves the task to `merge`.',
        'Record the reply as a Decisions row quoting it.',
        "`fix` (the user's disposition): send those findings to the owning session with `Plan: enter plan mode (EnterPlanMode) for this task before anything else; write nothing until the user approves the plan.`; the task goes to `fix`, then `verify` (see Pipeline).",
        '`file`: file the issue (see Tracker).',
        'When the Settings TOML names `[workflow] architecture_reviewer`, send an architecture-axis finding to that session too.',
        'If it names none, the filed issue remains in the backlog for ordinary assignment.',
        '`keep` and `discard`: the Decisions row is the record.',
    ),
}
# Slice 6 deletes no rule outright; all other original sentences must move.
REVIEW_PIPELINE_DELETED_RULES = ()
REVIEW_PIPELINE_REFERENCES = {'Asks': ("A page that already exists is used, not rewritten: a reviewer's findings comment is the page for triage (see Triage), with or without a `Backlog:`.",)}


# Captured once from git show origin/main:agents/chief-of-stuff.md for slice 7.
# Strip list prefixes, then split sentence punctuation outside inline code;
# these static pins must never be regenerated from the current agent or skill.
COORDINATOR_SESSIONS_POINTER = "Before polling, assigning to, briefing, or sending to a session, spawning one, writing a Sessions row, reading a session's registration or report, or telling the user a session is ready for decommissioning, Read ${CLAUDE_PLUGIN_ROOT}/skills/coordinator-sessions/SKILL.md"
SESSIONS_RETAINED_PARAGRAPHS = ('For registered workers, check process state with `chief-of-stuff processes --root <workspace> [PID ...]`. '
 'Omit PIDs to check every registered worker; pass a space- or comma-separated batch when reconciling '
 "tracker PIDs. The TOON result gives each PID's assigned name, runtime and status. `running` verifies a "
 "registered worker's command and worktree; `gone` means the PID no longer exists; `pid_reused` means it "
 'belongs to another process; `unverified` means its identity could not be checked. A running PID without a '
 'registration proves only that a process exists. Use this helper instead of ad hoc `ps` or `pgrep` calls, '
 'and do not mark an `unverified` worker gone. Claude native sessions still use their native listing when '
 'available.',
 '- A listing is not a roster. A Sessions row exists for a session you spawned or handed work to, and for no '
 "other: a listing also shows sessions that are nobody's business here, and tooling that mints short-lived "
 'sessions of its own. Those rows own no task, answer no poll, and vanish like departed sessions. The orphan '
 'check below starts from task owners for this reason, and never from the listing.',
 "- Every time you list, check each task's owner against the list. An owner that is listed nowhere and has a "
 "ref is gone: set that task's state to `orphaned`, add a Log line naming the session, and tell the user "
 'once, with what is at risk — the worktree and paths from File ownership, and whether they hold uncommitted '
 'work. An orphaned task keeps its owner column as a record. Never send to a session that is not listed, and '
 "never re-dispatch an orphaned task on your own: its owner is the user's to decide.")
COORDINATOR_SESSIONS_RULES = {'Assign': ('In a one-shot workspace, use Dispatch for every task, including reviews and follow-up fixes; '
            'never hand a new task to a continuing session.',
            'The rules below apply only to interactive work.',
            'Assigning work to a live session is not a dispatch: the session already exists and runs under '
            "the user's own rules.",
            'Who takes a task is yours to decide, never a question for the user (see Asks): a live session '
            'whose last task is closed and whose role, as its name gives it, fits the task — or the session '
            'the user names.',
            'Only an `unassigned` task — one with its issue filed, where the block names a backlog (see '
            'Tracker).',
            'When you pick, never hand: anything with a decision inside it (a rename is a naming call, not a '
            'cleanup); anything touching a file a live session owns; anything on the human-only list; '
            "today's log, tracker or board; and any `orphaned` task whose tree holds uncommitted work.",
            'That last one reads small on the board and is not — picking it up is a rebase and an ownership '
            "call, and it is the user's.",
            'A task the user hands to a session they name is their call already made.',
            'Poll that session first and stop there.',
            'Say nothing about the item in that message; its answer may be that it is busy or constrained.',
            'On its reply — idle, or its last task closed — send the assignment (when the user named the '
            'session, add the Decisions row quoting them first): the whole ask in the first line, then '
            '`Name: <its name>. Use it everywhere; never take another.`, `Plan: enter plan mode '
            '(EnterPlanMode) for this task before anything else; write nothing until the user approves the '
            'plan.` (the user, 2026-09-23 22:25: "tasks passed to implementers should cause the implementer '
            'to enter plan mode for the new task"), `Lands by: follow [workflow] delivery: open a pull '
            'request and report its URL and head sha, or report the branch and head sha when delivery is '
            'branch.`, `Works under: follow the configured reviewer and merge policy.`, `Tracker: <path>`, '
            '`Owns: <paths>. Do not touch any other file.`, `Report: <what to reply with when done>`.',
            'Nothing about your own limits, and no "write only" line: what that session may run is between '
            'it and the user.',
            "Set the task's owner to the session and its state to `running HH:MM`, fill its Sessions row, "
            'and append a Log line.',
            'A reply that says the session is busy hands it nothing; the next check polls it again (see '
            'Check).',
            'The poll is how you know the last task closed — handing work to a session mid-task is the '
            'failure it exists for.'),
 'Brief': ('A brief is context, not an instruction to start: use it when the user asks you to bring a '
           'session up to speed.',
           'One message.',
           'Its first line is the whole ask ("brief on X so you can pick it up if the user says so"), its '
           'second is `Name: <its name>. Use it everywhere; never take another.`, and its third is `Works '
           'under: follow the configured reviewer and merge policy.` Context is file paths — the tracker, '
           "the design, the plan — never their contents pasted in, and never the coordinator's own limits.",
           'Then a Tasks row if the work is not already one, and a Log line naming who was briefed and on '
           'what.',
           'A brief is not a dispatch proposal and needs no yes: it hands over reading, not work.'),
 'Sessions': ("When the block has a `Sessions:` line naming a list tool and a send tool, the user's other "
              'Claude sessions are working sessions, and the tracker has a `## Sessions` table: `| ref | '
              'name | state | doing | waiting on | free at | constraints | children | last reply |`.',
              'The ref is the six hex characters in `name [ref]` from the list; it is the key.',
              'The name is the one the session was given — at launch with `--name`, or by the user with '
              "`/rename` — and it does not change: the row's `name` cell and every task owner are written "
              'with it and never rewritten to a name a listing shows.',
              "A session's worktree is a different string, and so is its ref.",
              "Update a session's row from its direct replies only; `last reply` is your clock read when the "
              'reply arrived, never a time the reply states.',
              '`state` is one of `planning`, `working`, `waiting`, `idle` and nothing else.',
              'It is what the session says about itself, so write only what it reported.',
              'Three more states are yours to work out for your reply and for the board, and never for the '
              'cell: a row with no ref yet is `starting`, a `doing` carrying `ready for decommissioning` is '
              '`ready`, and an owner the listing no longer shows is `gone`.',
              "The cell keeps the session's last report, blank if none; the board derives `gone` from the "
              'orphaned task.',
              '`waiting on` names who first, then why, separated by a dash: `<user> — the yes on S2`.',
              'A cell that only says why waits on nobody.',
              '`children` is what that session reports about its own subagents, and `none` when it says it '
              'has none.',
              'Blank means you have not asked.',
              'A subagent has no ref, is in no listing, answers no poll and cannot be sent to, so it never '
              'gets a row of its own — a row would say it can be reached.',
              'List before every send.',
              'Send to the name as listed, or `name [ref]`.',
              "A listing, or a message's sender, that shows a session under a name other than the one it was "
              'given is the harness titling it from its first task, not a rename.',
              'Keep the row and the owner cells as they are and send by `name [ref]` as listed.',
              "In the move where you first see it — list to match the ref if a sender's name is all you have "
              '— do two things once, and ask nobody first, because neither changes anything but what the '
              'session calls itself: send the session `Name: <its name>. Use it everywhere; never take '
              'another.`, and tell the user in that reply that its tab needs `/rename <name>` — a session '
              'cannot rename itself, and only the user can.',
              'Dual-transport sending: When sending to a session (poll, assignment, instruction, or reply), '
              'attempt direct messaging (`send`) first.',
              "If the send tool is missing, denied, or returns an error ('no agent reachable'), fall back "
              'immediately to the mailbox: `chief-of-stuff inbox send --to <recipient> --from coordinator '
              '--type <type> --body "<message>"`.',
              'A status poll is one message: `Reply in 6 lines: current task, and one of planning, working, '
              'waiting, idle; waiting on whom, and for what; when free; blocked by a permission prompt or '
              'classifier, on what; standing constraints <user> has given you; any subagents you have '
              'running now, and what each is doing.` The lines fill `doing` and `state`, `waiting on`, `free '
              'at`, the block relay, `constraints`, and `children` in that order.',
              "When a session's state is in question, poll it; do not ask the user.",
              'A session you spawned gets its Sessions row at once, with the spawn time in `last reply` and '
              '`ref` empty.',
              "The ref arrives in that session's **registration**: its assignment tells it to look itself up "
              'and send you one message carrying its ref, its worktree and branch, its task, and that it is '
              'planning with nothing written yet.',
              'Write the ref into the row from that message, set `doing` to what it said and `state` to '
              '`planning`, and leave its task where it is — registering is not progress.',
              'A row that has never carried a ref is not gone, it is not there yet: leave its task alone.',
              'If it still has no ref an hour later it never registered — say that, which is a different '
              'fact from `orphaned`, and the tree it was given is still on disk.',
              "Dual-transport registration: A session's registration may arrive via direct peer messaging "
              '(IPC) OR via the coordinator inbox (`chief-of-stuff inbox`).',
              'When polling or reviewing sessions, check `chief-of-stuff inbox list --recipient coordinator '
              '--unread`.',
              'If a registration message is present, write the ref into `## Sessions`, set `doing` to what '
              'it said, `state` to `planning`, and acknowledge the message with `chief-of-stuff inbox read '
              '<id> --ack`.',
              '`doing` carries `ready for decommissioning HH:MM` when a `task` session reports its task '
              'finished.',
              'It is not a task state and never becomes one.',
              'Before you tell the user, run the task audit for that tree: closing the terminal ends the '
              'only session that can merge that branch, so the reply says what is unmerged or uncommitted '
              'there.',
              'Closing it is theirs; you never close a session, remove a tree, or delete a branch.',
              'An agy session is in no native listing and cannot be sent to or polled directly.',
              'Its row carries `agy` in `ref`, and its ref is its name; it registers, asks, reports and '
              'stops only through the inbox, and everything you send it goes through `inbox.py send --to '
              '<its name>`.',
              'Check its registered PID with `process_status.py` for the orphan check.',
              "A poll to it is a mailbox message, and its row's `waiting on` reads `<its name> — its mailbox "
              'reply` until the reply arrives.')}
CHECK_RELAY_SHA256 = {'Check': 'b3f92c4dcf9abe5f37ca9d7ba713ab10b8a56fb89e2c81bfcd742acbf3dd1ef9',
 'Relay': 'c053b7a3de53d53870a2fe3475cc3cc8777916dc8c5d9dc43db9adb543b6fef1'}


def rendered_sizes(pad: int, source: Path = ROOT) -> dict:
    """Each host's prompt size with the release and workspace paths taken out, so a short CI path
    and a long checkout path budget the same. `pad` lengthens both paths."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / ("w" * pad) / "workspace"
        root.mkdir(parents=True)
        release = start.install(source, base / ("i" * pad))
        sizes = {}
        for runtime in runtimes.NAMES:
            text = start.prompt(runtime, release, root).replace(str(release), "").replace(str(root), "")
            sizes[runtime] = len(text.encode())
        return sizes


def assert_skill_budgets(test: unittest.TestCase, root: Path, ceilings: dict, total: int):
    found = {p.parent.name: len(p.read_bytes()) for p in sorted((root / "skills").glob("*/SKILL.md"))}
    test.assertEqual(set(found), set(ceilings), "each skill needs an explicit budget")
    for name, size in found.items():
        test.assertLessEqual(size, ceilings[name], f"skill {name} exceeds its byte ceiling")
    test.assertLessEqual(sum(found.values()), total, "skills exceed their total byte ceiling")


def description(text: str) -> str:
    """Read the description key only in leading frontmatter, including folded/literal prose.

    Skill descriptions use plain or quoted strings, or YAML's indented block notation.
    For budgeting, join nonempty scalar lines with one space, including literal blocks.
    Searching the whole file would count an example's description as metadata.
    """
    front = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", text, flags=re.S)
    if not front:
        return ""
    lines = front[1].splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^description:[ \t]*(.*)$", line)
        if not match:
            continue
        value = match[1].strip()
        parts = [] if value in ("", ">", ">-", ">+", "|", "|-", "|+") else [value]
        for continuation in lines[index + 1:]:
            if not continuation.strip():
                continue
            if not continuation.startswith((" ", "\t")):
                break
            parts.append(continuation.strip())
        return " ".join(parts).strip("\"'")
    return ""


def assert_skill_descriptions(test: unittest.TestCase, root: Path):
    for path in sorted((root / "skills").glob("*/SKILL.md")):
        test.assertLessEqual(len(description(path.read_text())), DESCRIPTION_CAP,
                             f"skill {path.parent.name} description exceeds {DESCRIPTION_CAP} characters")


def assert_skill_pointers(test: unittest.TestCase, root: Path):
    text = (root / "agents" / "chief-of-stuff.md").read_text()
    for name in SKILL_POINTER.findall(text):
        test.assertTrue((root / "skills" / name / "SKILL.md").is_file(),
                        f"agent pointer names missing skill {name}")


class AgentBudgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw = (ROOT / "agents" / "chief-of-stuff.md").read_bytes()  # bytes: CRLF or locale cannot hide growth
        cls.size = len(raw)
        cls.text = raw.decode()

    def test_total(self):
        self.assertLessEqual(self.size, TOTAL)

    def test_each_section_is_within_its_ceiling(self):
        found = sections(self.text)
        self.assertEqual(set(CEILING), set(found), "a new section needs a budget line, a removed one loses its line")
        for name, text in found.items():
            with self.subTest(section=name):
                self.assertLessEqual(len(text.encode()), CEILING[name])

    def test_a_fenced_heading_is_not_a_section(self):
        self.assertEqual(sections("## A\nx\n```\n## B\n```\ny\n"), {"A": "## A\nx\n```\n## B\n```\ny\n"})
        with self.assertRaises(ValueError):
            sections("## A\n## A\n")
        # The Resume code fence holds a literal `## Resume` line; a naive parse restarts the count there.
        self.assertGreater(len(sections(self.text)["Resume"].encode()), 4000)

    def test_no_line_outgrows_the_cap(self):
        for line in self.text.split("\n"):
            self.assertLessEqual(len(line.encode()), MAX_LINE, line[:60])

    def test_the_one_shot_paragraph_is_capped(self):
        self.assertLessEqual(len(one_shot_paragraph(rules_text()).encode()), ONE_SHOT_PARAGRAPH)

    def test_every_hosts_rendered_prompt_is_capped_whatever_the_path_length(self):
        short, long = rendered_sizes(1), rendered_sizes(40)
        self.assertEqual(short, long)
        self.assertEqual(set(PROMPT), set(short))
        for runtime, size in short.items():
            with self.subTest(runtime=runtime):
                self.assertLessEqual(size, PROMPT[runtime])

    def test_each_skill_and_the_total_are_within_their_byte_ceilings(self):
        assert_skill_budgets(self, ROOT, SKILL_CEILING, SKILLS_TOTAL)

    def test_every_skill_frontmatter_description_is_capped(self):
        assert_skill_descriptions(self, ROOT)

    def test_every_agent_skill_pointer_names_an_existing_skill(self):
        assert_skill_pointers(self, ROOT)

    def test_decision_page_pointer_occurs_exactly_once_at_the_point_of_use(self):
        self.assertEqual(self.text.count(DECISION_PAGE_POINTER), 1)
        self.assertIn(DECISION_PAGE_POINTER, self.text.splitlines(), "pointer must be a one-line imperative")
        self.assertIn(DECISION_PAGE_POINTER, sections(self.text)["Asks"])

    def test_decision_page_skill_exists(self):
        self.assertTrue((ROOT / "skills" / "decision-page" / "SKILL.md").is_file(),
                        "decision-page skill is missing")

    def test_optional_feature_pointers_are_the_only_content_in_their_sections(self):
        found = sections(self.text)
        for name, pointer in OPTIONAL_FEATURE_POINTERS.items():
            with self.subTest(section=name):
                self.assertEqual(self.text.count(pointer), 1)
                self.assertEqual(self.text.splitlines().count(pointer), 1,
                                 "pointer must be a standalone imperative with no final period")
                lines = [line for line in found[name].splitlines() if line.strip()]
                self.assertEqual(lines, [f"## {name}", pointer],
                                 "keep the heading and exactly one pointer line, with nothing else")

    def test_assign_keeps_only_the_standalone_sessions_pointer(self):
        pointer = COORDINATOR_SESSIONS_POINTER
        self.assertEqual(self.text.count(pointer), 1)
        self.assertEqual(self.text.splitlines().count(pointer), 1)
        self.assertIn(pointer, sections(self.text)["Assign"].split("\n\n"))
        self.assertEqual([line for line in sections(self.text)["Assign"].splitlines() if line.strip()],
                         ["## Assign", pointer])
        self.assertNotIn("Brief", sections(self.text))
        for anchor in ("list sessions;", "list sessions,"):
            self.assertNotIn(anchor, pointer, "host replacement anchors must never consume the pointer")

    def test_sessions_keeps_exactly_the_three_retained_paragraphs_in_order(self):
        expected = "## Sessions\n\n" + "\n\n".join(SESSIONS_RETAINED_PARAGRAPHS)
        self.assertEqual(sections(self.text)["Sessions"].strip(), expected)

    def test_check_sessions_relay_stay_adjacent_and_check_relay_are_untouched(self):
        names = list(sections(self.text))
        index = names.index("Check")
        self.assertEqual(names[index:index + 3], ["Check", "Sessions", "Relay"],
                         "start_coordinator.py replaces Check/Sessions using these adjacent headings")
        for name, digest in CHECK_RELAY_SHA256.items():
            self.assertEqual(hashlib.sha256(sections(self.text)[name].encode()).hexdigest(), digest,
                             f"slice 7 must leave {name} verbatim")

    def test_coordinator_sessions_skill_has_portable_frontmatter_and_topics(self):
        path = ROOT / "skills" / "coordinator-sessions" / "SKILL.md"
        self.assertTrue(path.is_file(), "coordinator-sessions skill is missing")
        text = path.read_text()
        front = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", text, flags=re.S)
        self.assertIsNotNone(front, "coordinator-sessions needs leading YAML frontmatter")
        self.assertRegex(front[1], r'''(?m)^name:[ \t]*(?:coordinator-sessions|"coordinator-sessions"|'coordinator-sessions')[ \t]*$''')
        self.assertNotRegex(front[1], r"(?m)^\s*hosts\s*:")
        prose = description(text)
        self.assertTrue(prose)
        self.assertLessEqual(len(prose), DESCRIPTION_CAP)
        for moment in (r"poll", r"assign", r"brief", r"send", r"spawn", r"Sessions row", r"registration", r"report", r"decommission"):
            with self.subTest(moment=moment):
                self.assertRegex(prose, re.compile(moment, re.I))
        body = skill_body(text)
        self.assertEqual(re.findall(r"(?m)^### (.*)$", body), ["Sessions", "Assign", "Brief"])
        self.assertNotRegex(body, r"(?m)^## (?:Sessions|Assign|Brief)$")

    def test_every_sessions_assign_brief_sentence_moves_once_into_the_skill(self):
        path = ROOT / "skills" / "coordinator-sessions" / "SKILL.md"
        self.assertTrue(path.is_file(), "coordinator-sessions skill is missing")
        body, combined = skill_body(path.read_text()), rules_text()
        for topic, sentences in COORDINATOR_SESSIONS_RULES.items():
            for sentence in sentences:
                with self.subTest(topic=topic, sentence=sentence):
                    self.assertEqual(body.count(sentence), 1)
                    self.assertEqual(combined.count(sentence), 1)
                    self.assertNotIn(sentence, self.text)
        for paragraph in SESSIONS_RETAINED_PARAGRAPHS:
            self.assertEqual(self.text.count(paragraph), 1)
            self.assertEqual(combined.count(paragraph), 1)
            self.assertNotIn(paragraph, body)

    def test_sessions_and_check_cross_references_resolve_and_reject_broken_copies(self):
        path = ROOT / "skills" / "coordinator-sessions" / "SKILL.md"
        self.assertTrue(path.is_file(), "coordinator-sessions skill is missing")
        body = skill_body(path.read_text())

        def resolve(agent, skill):
            found = sections(agent)
            for target in re.findall(r"\(see (Sessions|Check)\)", agent + "\n" + skill):
                self.assertIn(target, found, f"(see {target}) needs ## {target}")
                if target == "Sessions":
                    self.assertIn(SESSIONS_RETAINED_PARAGRAPHS[2], found[target],
                                  "(see Sessions) needs the retained orphan rule")
            if "Assign step 1" in agent:
                self.assertIn(COORDINATOR_SESSIONS_POINTER, found.get("Assign", ""),
                              "Assign step 1 needs the Assign pointer")
                self.assertIn("### Assign", skill.splitlines(), "Assign step 1 needs ### Assign")
                self.assertIn("1. Poll that session first and stop there.", skill,
                              "Assign step 1 needs the moved first step")
            if "orphan check below" in agent:
                session = found.get("Sessions", "")
                self.assertIn(SESSIONS_RETAINED_PARAGRAPHS[2], session, "orphan check below needs the orphan rule")
                self.assertLess(session.index("orphan check below"), session.index("Every time you list,"))

        for reference, count in {"(see Sessions)": 2, "(see Check)": 4,
                                 "Assign step 1": 1, "orphan check below": 1}.items():
            self.assertEqual(self.text.count(reference), count, "keep the agent references verbatim")
        mutations = (
            (self.text.replace("## Sessions\n", "## Registry\n"), body),
            (self.text.replace("## Check\n", "## Schedule\n"), body),
            (self.text.replace(COORDINATOR_SESSIONS_POINTER, "Read elsewhere"), body),
            (self.text, body.replace("### Assign\n", "### Hand-off\n")),
            (self.text, body.replace("1. Poll that session first and stop there.", "1. Send immediately.")),
            (self.text.replace(SESSIONS_RETAINED_PARAGRAPHS[2], "Orphans omitted."), body),
            (self.text.replace("\n\n".join(SESSIONS_RETAINED_PARAGRAPHS[1:]),
                               "\n\n".join(reversed(SESSIONS_RETAINED_PARAGRAPHS[1:]))), body),
        )
        for agent, skill in mutations:
            with self.subTest(mutation=agent != self.text, skill_mutation=skill != body):
                with self.assertRaises(AssertionError):
                    resolve(agent, skill)
        resolve(self.text, body)

    def test_state_report_requirements_file_refers_to_requirements_section(self):
        authority = sections(self.text)["Write authority"]
        state_report = next(paragraph for paragraph in authority.split("\n\n")
                            if paragraph.startswith("A state report is "))
        self.assertIn("a requirements file (see Requirements)", state_report)

    def test_optional_features_skill_exists_with_portable_frontmatter(self):
        path = ROOT / "skills" / "optional-features" / "SKILL.md"
        self.assertTrue(path.is_file(), "optional-features skill is missing")
        text = path.read_text()
        front = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", text, flags=re.S)
        self.assertIsNotNone(front, "optional-features needs leading YAML frontmatter")
        self.assertRegex(front[1], r'''(?m)^name:[ \t]*(?:optional-features|"optional-features"|'optional-features')[ \t]*$''')
        self.assertNotRegex(front[1], r"(?m)^\s*hosts\s*:")
        prose = description(text)
        self.assertTrue(prose, "optional-features needs a description")
        self.assertLessEqual(len(prose), DESCRIPTION_CAP)
        for topic in (r"\bPARA filing\b", r"\brequirements checklists\b", r"\bnotifications\b"):
            with self.subTest(topic=topic):
                self.assertRegex(prose, re.compile(topic, re.I))

    def test_optional_feature_rule_sentences_are_single_sourced_in_the_skill_body(self):
        path = ROOT / "skills" / "optional-features" / "SKILL.md"
        self.assertTrue(path.is_file(), "optional-features skill is missing")
        body = skill_body(path.read_text())
        for name, sentences in OPTIONAL_FEATURE_RULES.items():
            for sentence in sentences:
                with self.subTest(section=name, sentence=sentence):
                    self.assertEqual(body.count(sentence), 1, "preserve the verbatim rule in the skill body")
                    self.assertNotIn(sentence, self.text, "moved rules must no longer live in the agent")

    def test_tracker_rows_pointer_occurs_once_as_its_own_paragraph_in_tracker(self):
        self.assertEqual(self.text.count(TRACKER_ROWS_POINTER), 1)
        self.assertEqual(self.text.splitlines().count(TRACKER_ROWS_POINTER), 1,
                         "pointer must be a standalone imperative with no final period")
        self.assertIn(TRACKER_ROWS_POINTER, sections(self.text)["Tracker"].split("\n\n"),
                      "the pointer must be its own paragraph inside Tracker")

    def test_tracker_lane_stage_fragment_is_absent_from_agent_and_rules_text(self):
        for source, text in (("agent", self.text), ("rules_text", rules_text())):
            with self.subTest(source=source):
                self.assertNotIn("New: `lane`, `stage`.", text)

    def test_tracker_rows_skill_exists_with_portable_frontmatter(self):
        path = ROOT / "skills" / "tracker-rows" / "SKILL.md"
        self.assertTrue(path.is_file(), "tracker-rows skill is missing")
        text = path.read_text()
        front = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", text, flags=re.S)
        self.assertIsNotNone(front, "tracker-rows needs leading YAML frontmatter")
        self.assertRegex(front[1], r"(?m)^name:[ \t]*(?:tracker-rows|\"tracker-rows\"|'tracker-rows')[ \t]*$")
        self.assertNotRegex(front[1], r"(?m)^\s*hosts\s*:")
        prose = description(text)
        self.assertTrue(prose, "tracker-rows needs a description")
        self.assertLessEqual(len(prose), DESCRIPTION_CAP)

    def test_tracker_row_rule_sentences_are_single_sourced_in_the_skill_body(self):
        path = ROOT / "skills" / "tracker-rows" / "SKILL.md"
        self.assertTrue(path.is_file(), "tracker-rows skill is missing")
        body, combined = skill_body(path.read_text()), rules_text()
        for opening in (
            "A `|` inside any cell is written", "A task whose row has no name still draws",
            "The optional `<sha>` is the commit", "Write it only when you have it",
            "`size` is your judgement", "A size arrives when you write the row",
            "`since` and `due` are what the board draws from",
            "Never set the cell by hand or write that line as free text",
        ):
            with self.subTest(opening=opening):
                self.assertNotIn(opening, self.text)
                self.assertIn(opening, body)
                self.assertIn(opening, combined)
        for topic, sentences in TRACKER_ROW_RULES.items():
            for sentence in sentences:
                with self.subTest(topic=topic, sentence=sentence):
                    self.assertEqual(body.count(sentence), 1, "preserve the verbatim rule in the skill body")
                    self.assertNotIn(sentence, self.text, "moved rules must no longer live in the agent")
                    self.assertIn(sentence, combined)

    def test_tracker_state_done_and_widening_rules_stay_in_the_agent(self):
        tracker = sections(self.text)["Tracker"]
        for sentence in TRACKER_RETAINED_RULES:
            with self.subTest(sentence=sentence):
                self.assertIn(sentence, tracker)

    def test_resume_step_2_names_the_audits_issue_state_read(self):
        # PR #514 removed Tracker's issue-state audit sentence; live Sonnet skipped
        # the audit in 2/3 resume-closes-task-with-closed-issue runs. Pin it in step 2.
        resume = sections(self.text)["Resume"]
        self.assertEqual(resume.count("and from the audit, which reads every issue's state (see Tracker);"), 1)

    def test_tracker_redundant_audit_sentences_are_absent_from_rules_text(self):
        text = rules_text()
        for sentence in TRACKER_DELETED_RULES:
            with self.subTest(sentence=sentence):
                self.assertNotIn(sentence, text)

    def test_review_pipeline_pointer_occurs_once_as_its_own_paragraph(self):
        self.assertEqual(self.text.count(REVIEW_PIPELINE_POINTER), 1)
        self.assertEqual(self.text.splitlines().count(REVIEW_PIPELINE_POINTER), 1,
                         "pointer must be a standalone imperative with no final period")
        self.assertIn(REVIEW_PIPELINE_POINTER, sections(self.text)["Pipeline"].split("\n\n"))

    def test_pipeline_keeps_only_gate_merge_guardrails_and_pointer(self):
        found = sections(self.text)
        self.assertNotIn("Triage", found, "Triage is now an on-demand topic")
        self.assertEqual([line for line in found["Pipeline"].splitlines() if line.strip()], [
            "## Pipeline", REVIEW_GATE_BULLET,
            "- " + " ".join(REVIEW_RETAINED_MERGE), REVIEW_PIPELINE_POINTER,
        ])

    def test_review_gate_bullet_and_no_cli_merge_rule_stay_in_agent(self):
        pipeline = sections(self.text)["Pipeline"]
        skill = (ROOT / "skills" / "review-pipeline" / "SKILL.md").read_text()
        for rule in (REVIEW_GATE_BULLET, *REVIEW_RETAINED_MERGE):
            self.assertEqual(self.text.count(rule), 1, "retained guardrails occur once in the agent")
            self.assertEqual(pipeline.count(rule), 1, "retained guardrails live in the agent itself")
            self.assertNotIn(rule, skill, "retained guardrails must be absent from the skill")
            self.assertEqual(rules_text().count(rule), 1, "do not copy retained guardrails into the skill")
        self.assertIn(" ".join(REVIEW_RETAINED_MERGE), pipeline,
                      "keep the verbatim merge sentences adjacent, with the wording ban last")

    def test_review_pipeline_skill_exists_with_portable_frontmatter_and_topic_headings(self):
        path = ROOT / "skills" / "review-pipeline" / "SKILL.md"
        self.assertTrue(path.is_file(), "review-pipeline skill is missing")
        text = path.read_text()
        front = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", text, flags=re.S)
        self.assertIsNotNone(front, "review-pipeline needs leading YAML frontmatter")
        self.assertRegex(front[1], r"(?m)^name:[ \t]*(?:review-pipeline|\"review-pipeline\"|'review-pipeline')[ \t]*$")
        self.assertNotRegex(front[1], r"(?m)^\s*hosts\s*:")
        self.assertTrue(description(text), "review-pipeline needs a description")
        self.assertLessEqual(len(description(text)), DESCRIPTION_CAP)
        body = skill_body(text)
        self.assertGreaterEqual(len(re.findall(r"(?m)^### \S.*$", body)), 2,
                                "organize the moved rules with ### topic headings")
        self.assertNotRegex(body, r"(?m)^## (?:Pipeline|Triage)$",
                            "do not reintroduce agent sections in combined rules_text")

    def test_review_pipeline_sentences_survive_once_in_rules_text(self):
        combined = rules_text()
        for topic, sentences in REVIEW_PIPELINE_RULES.items():
            for sentence in sentences:
                with self.subTest(topic=topic, sentence=sentence):
                    self.assertIn(sentence, combined)
                    self.assertEqual(combined.count(sentence), 1, "preserve every moved sentence exactly once")
        for sentence in REVIEW_PIPELINE_DELETED_RULES:
            self.assertNotIn(sentence, combined)

    def test_review_pipeline_sentences_move_out_of_agent_into_skill(self):
        path = ROOT / "skills" / "review-pipeline" / "SKILL.md"
        self.assertTrue(path.is_file(), "review-pipeline skill is missing")
        body = skill_body(path.read_text())
        for topic, sentences in REVIEW_PIPELINE_RULES.items():
            for sentence in sentences:
                with self.subTest(topic=topic, sentence=sentence):
                    self.assertEqual(body.count(sentence), 1, "preserve the verbatim sentence in the skill")
                    self.assertNotIn(sentence, self.text, "moved prose must leave the agent")

    def test_other_sections_keep_pipeline_and_triage_references_that_resolve(self):
        found = sections(self.text)
        for name, references in REVIEW_PIPELINE_REFERENCES.items():
            for reference in references:
                self.assertIn(reference, found[name], "leave other sections' references verbatim")

        def assert_references_resolve(agent, skill):
            agent_sections = sections(agent)
            for target in re.findall(r"\(see (Pipeline|Triage)\)", agent):
                if target == "Pipeline":
                    self.assertIn("Pipeline", agent_sections, "(see Pipeline) needs ## Pipeline in the agent")
                else:
                    self.assertIn("### Triage", skill_body(skill).splitlines(),
                                  "(see Triage) needs ### Triage in the skill")
                    self.assertIn(REVIEW_PIPELINE_POINTER, agent_sections.get("Pipeline", "").split("\n\n"),
                                  "(see Triage) needs the standalone review-pipeline pointer")

        skill = (ROOT / "skills" / "review-pipeline" / "SKILL.md").read_text()
        self.assertIn("(see Triage)", self.text, "the heading mutation must exercise a reference")
        renamed_skill = skill.replace("### Triage\n", "### Findings\n")
        with self.assertRaisesRegex(AssertionError, "needs ### Triage"):
            assert_references_resolve(self.text, renamed_skill)
        assert_references_resolve(self.text, skill)

    def test_skill_budget_controls_reject_unbudgeted_and_oversized_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_plugin(Path(tmp))
            path = write_skill(root, "alpha", "A generic rule: é.", "description: Use for a generic task.")
            size = len(path.read_bytes())
            self.assertGreater(size, len(path.read_text()), "budget controls exercise bytes, not characters")
            assert_skill_budgets(self, root, {"alpha": size}, size)
            with self.assertRaisesRegex(AssertionError, "explicit budget"):
                assert_skill_budgets(self, root, {}, size)
            with self.assertRaisesRegex(AssertionError, "alpha exceeds its byte ceiling"):
                assert_skill_budgets(self, root, {"alpha": size - 1}, size)
            with self.assertRaisesRegex(AssertionError, "total byte ceiling"):
                assert_skill_budgets(self, root, {"alpha": size}, size - 1)

    def test_description_controls_use_the_frontmatter_key_and_character_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_plugin(Path(tmp))
            body = "## Generic rule\n\ndescription: " + "x" * 500
            for template in ("{}", '"{}"', "'{}'", ">-\n  {}", "|-\n  {}"):
                with self.subTest(template=template):
                    # Description is the first key, so a parser expecting a preceding newline fails.
                    path = write_skill(root, "alpha", body,
                                       "description: " + template.format("é" * DESCRIPTION_CAP) + "\nname: alpha")
                    self.assertEqual(description(path.read_text()), "é" * DESCRIPTION_CAP)
                    assert_skill_descriptions(self, root)
                    write_skill(root, "alpha", body,
                                "description: " + template.format("é" * (DESCRIPTION_CAP + 1)) + "\nname: alpha")
                    with self.assertRaisesRegex(AssertionError, "alpha description exceeds 300"):
                        assert_skill_descriptions(self, root)

    def test_multiline_description_controls_count_all_400_characters(self):
        # Budget normalization joins nonempty continuation lines with one space,
        # including literal blocks; this helper measures prose, not YAML rendering.
        expected = "é" * 199 + " " + "é" * 200
        templates = {
            "plain_continuation": "description: {first}\n  {second}",
            "value_on_next_line": "description:\n  {first}\n  {second}",
            "folded_blank_line": "description: >-\n  {first}\n\n  {second}",
            "literal_blank_line": "description: |-\n  {first}\n\n  {second}",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = write_plugin(Path(tmp))
            for form, template in templates.items():
                with self.subTest(form=form):
                    # The same multiline forms at the cap remain valid controls.
                    metadata = template.format(first="é" * 149, second="é" * 150) + "\nname: alpha"
                    path = write_skill(root, "alpha", "description: BODY_ONLY", metadata)
                    self.assertEqual(description(path.read_text()), "é" * 149 + " " + "é" * 150)
                    assert_skill_descriptions(self, root)
                    metadata = template.format(first="é" * 199, second="é" * 200) + "\nname: alpha"
                    path = write_skill(root, "alpha", "description: BODY_ONLY", metadata)
                    value = description(path.read_text())
                    self.assertEqual(len(value), 400)
                    self.assertEqual(value, expected, "do not count the following key or body")
                    with self.assertRaisesRegex(AssertionError, "alpha description exceeds 300"):
                        assert_skill_descriptions(self, root)

    def test_pointer_controls_require_a_skill_file_not_just_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_plugin(Path(tmp))
            with self.assertRaisesRegex(AssertionError, "missing skill alpha"):
                assert_skill_pointers(self, root)
            (root / "skills" / "alpha").mkdir(parents=True)
            with self.assertRaisesRegex(AssertionError, "missing skill alpha"):
                assert_skill_pointers(self, root)
            write_skill(root, "alpha", "Generic rule.")
            assert_skill_pointers(self, root)

    def test_rendered_budget_is_independent_of_on_demand_skill_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_plugin(Path(tmp))
            before = rendered_sizes(1, root)
            body = "## Generic detail\n\n" + "é" * 500
            write_skill(root, "alpha", body, "description: Use for generic detail.")
            after = rendered_sizes(1, root)
            self.assertEqual(after, rendered_sizes(40, root), "prompt bytes must be path independent")
            for runtime in runtimes.NAMES:
                with self.subTest(runtime=runtime):
                    self.assertEqual(after[runtime], before[runtime],
                                     "on-demand skill bodies must not consume starting prompt bytes")

    def test_non_claude_prompts_do_not_keep_claude_only_phrases(self):
        # start_coordinator.prompt() swaps these by exact-string .replace(); an edit to the agent
        # file that breaks the match would otherwise ship the Claude tool names to other hosts.
        with tempfile.TemporaryDirectory() as tmp:
            for runtime in ("codex", "cursor", "agy"):
                prompt = start.prompt(runtime, ROOT, Path(tmp))
                for phrase in ("CronCreate", "list sessions;"):
                    with self.subTest(runtime=runtime, phrase=phrase):
                        self.assertNotIn(phrase, prompt)


if __name__ == "__main__":
    unittest.main()
