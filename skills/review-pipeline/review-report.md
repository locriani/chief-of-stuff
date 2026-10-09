# Review report

Every agent that reviews a pull or merge request writes its report in this shape, whatever its runtime: the standing reviewer, a review one-shot, a review session. The coordinator's Triage reads R-numbers, axis, severity, confidence and the suggestion off it, so a report in another shape is one the coordinator cannot triage. Post the report as one comment on the pull or merge request, even when the task is otherwise read-only, and send the same text to the coordinator: in your report, or whole in a one-shot result's `reason`. Never approve, request changes, merge, or fix what you found.

The first line is always `Reviewer pass: <full|verify> @ <head sha>`; that line is how a report is told from any other message.

## Full pass

The first review of a pull or merge request.

```text
Reviewer pass: full @ <head sha>
PR: <url>
Tests: <pass|fail> — <counts> — `<command>`
Axes not examined: <axis: reason>, or none

R1 | <axis> | <severity> | <confidence> | <file:line> | <what> | <evidence> | suggested: <fix|file|keep|discard>
R2 | ...

Assessment: <one line on the change; no merge verdict>
Dispositions are the user's: file / keep / fix / discard.
```

## Verify pass

The second review. It covers only the R-numbers the coordinator sent as fixes, and the diff since the sha the full pass reviewed. A third pass needs the user's word.

```text
Reviewer pass: verify @ <head sha>
PR: <url>
Tests: <pass|fail> — <counts> — `<command>`
Axes examined: <axes>
Resolved: R<n> — <file:line>, <how it is fixed now>
Unresolved: R<n> — <file:line>, <what is still wrong>
Findings: none
```

New findings in that diff take the next R-numbers as rows, in place of `Findings: none`.

## Rows

- One row per finding, numbered `R1..Rn`, with tests that lie first. Rows have exactly eight fields separated by ` | `, so a field never contains a `|`.
- A finding without a `file:line` and its evidence is dropped, not reported. Evidence is the line, the command and its output, or the test that shows it.
- Axis names what was examined, such as Correctness, Security, Architecture compliance, Test run, Tests that lie, Maintainability. Name Security in the axis whenever the finding concerns security in substance: a path check, authentication, input handling, secrets or permissions.
- Severity is one of three words. `Critical` covers critical and high. `Important` covers medium. `Minor` covers low and nits.
- Confidence is never rounded up. `Confirmed` means reproduced or run. `Probable` means strong static evidence. `Unverified` means neither, and says what would settle it.
- The suggestion is a recommendation to the user. `fix` means fix it in this pull request. `file` means open an issue for later. `keep` means accept the code as it is, for a reason the row states. `discard` means you doubt the finding yourself.
- A pass with no findings is still reported, with `Findings: none` in place of rows.
