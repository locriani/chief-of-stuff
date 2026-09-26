#!/usr/bin/env python3
"""Run one scoped task in one CLI turn, then reconcile its result."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import backlog
import dispatch_prompt
import kanban
from _vendor.toon_format import decode as toon_decode, encode as toon_encode
from render_board import _cells, _is_separator, parse_tracker
from settings import load as load_settings
from shell_setup import clean_env, login_argv, resolve

RESULT = Path(dispatch_prompt.PROMPT_DIR) / "worker-result.toon"
REPORT = Path(dispatch_prompt.PROMPT_DIR) / "one-shot-report.toon"
BOOTSTRAP = ("Read {dispatch} first. It is your entire one-shot assignment. Work in {cwd}. "
             "Finish in this invocation and write the requested TOON result before exiting.")


def command(runtime: str, binary: str, cwd: Path, dispatch: Path, *, agent_type: str | None,
            model: str, effort: str) -> list[str]:
    """A single foreground CLI invocation, without interactive plan or mailbox modes."""
    prompt = BOOTSTRAP.format(dispatch=dispatch, cwd=cwd)
    if runtime == "claude":
        argv = [binary, "--print", "--permission-mode", "auto", "--permission-prompts", "none",
                "--no-session-persistence", "--plugin-dir", str(Path(__file__).resolve().parent.parent)]
        if agent_type:
            argv += ["--agent", agent_type]
        if model:
            argv += ["--model", model]
        if effort:
            argv += ["--effort", effort]
    elif runtime == "codex":
        # `never` returns blocked operations to the model instead of waiting for a human.
        argv = [binary, "-a", "never", "exec", "-C", str(cwd), "--sandbox", "workspace-write",
                "--ephemeral"]
        if model:
            argv += ["--model", model]
    elif runtime == "cursor":
        argv = [binary, "--print", "--force", "--trust", "--workspace", str(cwd)]
        if model:
            argv += ["--model", model]
    elif runtime == "agy":
        argv = [binary, "--print", "--mode", "accept-edits", "--dangerously-skip-permissions"]
        if model:
            argv += ["--model", model]
    else:
        raise ValueError(f"unknown runtime {runtime}")
    return [*argv, prompt]


def worker_result(path: Path, exit_code: int) -> tuple[str, str, str]:
    """Do not infer success from the CLI exit code or a free-form final response."""
    try:
        data = toon_decode(path.read_text())
    except (OSError, ValueError, TypeError) as exc:
        return "human_review", f"worker did not write a valid TOON result ({exc}); exit {exit_code}", ""
    if not isinstance(data, dict) or data.get("status") not in ("done", "human_review") or any(
        not isinstance(data.get(k), str) or not data[k].strip() for k in ("reason", "changes")
    ):
        return "human_review", f"worker result is incomplete or invalid; exit {exit_code}", ""
    if exit_code:
        return "human_review", f"worker exited {exit_code}: {data['reason']}", data["changes"]
    return data["status"], data["reason"], data["changes"]


def changed_files(cwd: Path) -> str:
    try:
        out = subprocess.run(["git", "status", "--short"], cwd=cwd, capture_output=True,
                             text=True, timeout=15, check=False)
        return out.stdout.strip() if out.returncode == 0 else f"git status failed: {out.stderr.strip()}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"git status failed: {exc}"


def git_head(cwd: Path) -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True,
                             text=True, timeout=15, check=False)
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def committed_changes(cwd: Path, before: str | None) -> str:
    after = git_head(cwd)
    if not before or not after or before == after:
        return "No new commits detected"
    try:
        out = subprocess.run(["git", "log", "--format=%h %s", "--stat", f"{before}..{after}"],
                             cwd=cwd, capture_output=True, text=True, timeout=15, check=False)
        return out.stdout.strip()[:12000] if out.returncode == 0 else f"Could not read commits: {out.stderr.strip()}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Could not read commits: {exc}"


def _brief(value: str) -> str:
    return " ".join(value.split())[:500]


def update_tracker(path: Path, task: str, name: str, owner: str, status: str, reason: str, changes: str) -> None:
    """Set the dispatched row to waiting and append a durable local result note."""
    text = path.read_text()
    rows = [row for row in parse_tracker(text).tasks if row.item.strip() == task]
    if len(rows) != 1 or rows[0].owner.strip().lower() != "unassigned":
        raise ValueError("task row changed during the one-shot run; tracker needs manual reconciliation")
    lines = text.splitlines(keepends=True)
    in_tasks = False
    hits = []
    for i, line in enumerate(lines):
        if line.startswith("## "):
            in_tasks = line.rstrip() in ("## Tasks", "## Lanes")
        if in_tasks and line.startswith("|") and not _is_separator(_cells(line)):
            cells = _cells(line)
            if task in cells:
                hits.append(i)
    if len(hits) != 1:
        raise ValueError("could not identify one task table row for reconciliation")
    i = hits[0]
    # Owner and state are adjacent in every supported tracker table. Replace only that
    # span; leave the item and every other cell byte-for-byte as approved.
    pattern = re.compile(r"\|\s*unassigned\s*\|\s*(?:open|waiting)\s*\|", re.I)
    if "|" in owner or "\n" in owner:
        raise ValueError("configured user name cannot be written as a tracker owner")
    replaced, count = pattern.subn(lambda _: f"| {owner} | waiting |", lines[i])
    if count != 1:
        raise ValueError("task owner/state is not an unassigned open row")
    lines[i] = replaced
    stamp = datetime.now().strftime("%H:%M")
    note = (f"- {stamp} one-shot {name}: {'completed; awaiting integration' if status == 'done' else 'HUMAN REVIEW NEEDED'}"
            f" — {_brief(reason)}. Changes: {_brief(changes)}.\n")
    log = next((j for j, line in enumerate(lines) if line.rstrip() == "## Log"), None)
    if log is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.extend(["\n## Log\n", "\n", note])
    else:
        end = next((j for j in range(log + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
        lines.insert(end, note)
    # A temporary sibling keeps readers from seeing a half-written tracker.
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", delete=False) as temp:
        temp.write("".join(lines))
        temporary = Path(temp.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def reconcile(root: Path, day: str, task: str, name: str, cwd: Path, exit_code: int,
              before_head: str | None = None) -> dict:
    cfg = dispatch_prompt._config(root)
    settings = load_settings(root, cfg.settings_path)
    status, reason, changes = worker_result(cwd / RESULT, exit_code)
    actual = changed_files(cwd)
    if status == "done" and actual:
        status = "human_review"
        reason = f"worker reported completion but the worktree is not clean: {actual}"
    summary = (changes + f"\nCommits from this run:\n{committed_changes(cwd, before_head)}" +
               (f"\nWorking tree:\n{actual}" if actual else "\nWorking tree clean"))
    rows = [row for row in parse_tracker((root / cfg.tracker_path(day)).read_text()).tasks if row.item.strip() == task]
    row = rows[0] if len(rows) == 1 else None
    errors = []
    if row is None or row.owner.strip().lower() != "unassigned" or row.kind != "open":
        report = {"status": "human_review", "task": task, "worker": name, "runtime_exit": exit_code,
                  "reason": "task row changed during the one-shot run; reconcile it manually",
                  "changes": summary, "errors": ["tracker: task row changed; no issue or tracker update was made"]}
        (cwd / REPORT).write_text(toon_encode(report) + "\n")
        return report
    ref = backlog.issue_ref(row.issue, cfg.backlog) if row and row.issue.strip() else None
    if status == "human_review" and ref:
        if settings.kanban:
            error = kanban.add_human_hold(ref, cfg.backlog, settings.kanban)
            if error:
                errors.append(f"review hold: {error}")
        else:
            errors.append("review hold: workspace has no [kanban] configuration")
        home = (backlog.GitHubBacklog(ref.repo) if ref.host == backlog.GITHUB else
                backlog.Backlog(cfg.backlog.host, ref.repo, cfg.backlog.env, cfg.backlog.account)
                if isinstance(cfg.backlog, backlog.Backlog) else None)
        if home is not None:
            body = f"One-shot worker {name} needs human review.\n\nWhy: {reason}\n\nChanges made:\n{summary}"
            commented = backlog.comment(home, ref.number, body, commit=True)
            if not commented.done:
                errors.append(f"issue comment: {commented.error}")
    try:
        update_tracker(root / cfg.tracker_path(day), task, name, cfg.user, status, reason, summary)
    except (OSError, ValueError) as exc:
        errors.append(f"tracker: {exc}")
    report = {"status": status, "task": task, "worker": name, "runtime_exit": exit_code,
              "reason": reason, "changes": summary, "errors": errors}
    (cwd / REPORT).write_text(toon_encode(report) + "\n")
    return report


def run(*, root: Path, day: str | None, task: str, cwd: Path, name: str,
        runtime: str, agent_type: str | None, model: str, effort: str, dry_run: bool,
        timeout_minutes: int = 60) -> int:
    cfg = dispatch_prompt._config(root)
    chosen_day = day or datetime.now(cfg.zone).date().isoformat()
    body = dispatch_prompt.compose(root, chosen_day, task, worktree=cwd, name=name,
                                   runtime=runtime, one_shot=True)
    binary = resolve({"cursor": "agent"}.get(runtime, runtime))
    if not binary:
        raise ValueError(f"{runtime} is unavailable in the configured interactive login shell")
    dispatch = cwd / dispatch_prompt.PROMPT_DIR / "dispatch.md"
    argv = command(runtime, binary, cwd, dispatch, agent_type=agent_type, model=model, effort=effort)
    if dry_run:
        print("would run one-shot: " + " ".join(shlex.quote(x) for x in argv))
        print(f"would write: {dispatch}")
        return 0
    if not cwd.is_dir():
        raise ValueError(f"no worktree at {cwd}")
    # Same clean login-shell path as interactive sessions; auth and user PATH come from shell setup.
    from spawn_session import write_dispatch
    write_dispatch(cwd, body)
    logs = cwd / dispatch_prompt.PROMPT_DIR
    before_head = git_head(cwd)
    env = clean_env()
    env["CHIEF_OF_STUFF_WORKSPACE"] = str(root.resolve())
    try:
        with (logs / "worker-stdout.log").open("w") as stdout, (logs / "worker-stderr.log").open("w") as stderr:
            completed = subprocess.run(login_argv(argv), cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, timeout=timeout_minutes * 60,
                                       check=False)
        exit_code = completed.returncode
    except subprocess.TimeoutExpired:
        with (logs / "worker-stderr.log").open("a") as stderr:
            stderr.write(f"One-shot run timed out after {timeout_minutes} minutes\n")
        exit_code = 124
    except OSError as exc:
        (logs / "worker-stderr.log").write_text(f"Could not start worker: {exc}\n")
        exit_code = 127
    report = reconcile(root, chosen_day, task, name, cwd, exit_code, before_head)
    print(toon_encode(report))
    return 0 if report["status"] == "done" and not report["errors"] else 1
