#!/usr/bin/env python3
"""Run one scoped task in one CLI turn, then reconcile its result."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import subprocess
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path

import backlog
import dispatch_prompt
import kanban
import ownership
import runtimes
import tracker_write
from _vendor.toon_format import ToonDecodeError, decode as toon_decode, encode as toon_encode
from md import cells as _cells, is_separator as _is_separator
from notify_service import atomic_write
from one_shot_report import FIELD_MAX, UNREADABLE, read_report
from process_status import PIDFILE, running_trees
from tracker import parse_tracker
from tracker_log import RELAUNCHED, ended_line, relaunch_line, started_line
from workspace import worktrees_dir
from settings import load as load_settings
from shell_setup import clean_env, login_argv, resolve

RESULT = Path(dispatch_prompt.RESULT_FILE)
REPORT = Path(dispatch_prompt.PROMPT_DIR) / "one-shot-report.toon"  # the tree's copy; `result` reads the launcher's own
NO_COMMITS = "No new commits detected"
BOOTSTRAP = ("Read {dispatch} first. It is your entire one-shot assignment. Work in {cwd}. "
             "Finish in this invocation and write the requested TOON result before exiting.")


def _own_git_dirs(cwd: Path) -> list[str]:
    """The git directories codex may write: a linked worktree's common dir and own gitdir, or a clone's `.git`."""
    # The worker can rewrite `.git`, `commondir`, and gitfiles, so a path is granted only at cwd's own toplevel, where `.git` and git agree.
    def rev(flag: str) -> Path:
        out = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", flag], env=clean_env(),
                             capture_output=True, text=True, check=False, timeout=15)
        return Path(out.stdout.strip()).resolve() if out.returncode == 0 and out.stdout.strip() else Path()

    try:
        here, top, common, gitdir = cwd.resolve(), rev("--show-toplevel"), rev("--git-common-dir"), rev("--absolute-git-dir")
        dotgit = here / ".git"
        if top != here or dotgit.is_symlink() or Path() in (common, gitdir):
            return []
        if dotgit.is_dir():  # a normal clone
            return [str(common)] if dotgit.resolve() == common == gitdir else []
        # commondir must name `common` and the back-pointer file must name this `.git`; a damaged file raises and grants nothing.
        owns = (gitdir / (gitdir / "commondir").read_text().strip()).resolve() == common \
            and (gitdir / (gitdir / "gitdir").read_text().strip()).resolve() == dotgit
        return [str(common), str(gitdir)] if owns and gitdir.parent == common / "worktrees" else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def command(runtime: str, binary: str, cwd: Path, dispatch: Path, *, agent_type: str | None,
            model: str, effort: str) -> list[str]:
    """A single foreground CLI invocation, without interactive plan or mailbox modes."""
    prompt = BOOTSTRAP.format(dispatch=dispatch, cwd=cwd)
    tail: list[str] = []
    if runtime == "claude":
        argv = [binary, "--print", "--permission-mode", "auto", "--permission-prompts", "none",
                "--no-session-persistence", "--plugin-dir", str(Path(__file__).resolve().parent.parent)]
        if agent_type:
            argv += ["--agent", agent_type]
    elif runtime == "codex":
        # `never` returns blocked operations to the model instead of waiting for a human.
        argv = [binary, "-a", "never", "exec", "-C", str(cwd), "--sandbox", "workspace-write",
                # Network for fetch, push, and forge APIs; file writes stay confined.
                "-c", "sandbox_workspace_write.network_access=true", "--ephemeral"]
        argv += [a for d in _own_git_dirs(cwd) for a in ("--add-dir", d)]
    elif runtime == "cursor":
        argv = [binary, "--print", "--force", "--trust", "--workspace", str(cwd)]
    elif runtime == "agy":
        argv = [binary, "--mode", "accept-edits", "--dangerously-skip-permissions"]
        # agy's --print takes the prompt as its value, so it goes last, right before the prompt.
        tail = ["--print"]
    else:
        raise ValueError(f"unknown runtime {runtime}")
    flags = [*(["--model", model] if model else []), *runtimes.get(runtime).effort_tokens(effort)]
    return [*argv, *flags, *tail, prompt]


def worker_result(path: Path, exit_code: int) -> tuple[str, str, str]:
    """Do not infer success from the CLI exit code or a free-form final response."""
    try:
        raw = path.read_text()
    except OSError as exc:
        return "human_review", f"worker did not write a valid TOON result ({exc}); exit {exit_code}", ""
    try:
        data = toon_decode(raw)
    except (ToonDecodeError, ValueError, TypeError) as exc:
        try:  # some workers write JSON instead of TOON (#154); same fields either way
            data = json.loads(raw)
        except ValueError:
            return "human_review", f"worker wrote an unreadable TOON result ({exc}); exit {exit_code}: {_brief(raw)}", ""
    if not isinstance(data, dict) or data.get("status") not in ("done", "human_review", "relaunch") or any(
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
        return NO_COMMITS
    try:
        out = subprocess.run(["git", "log", "--format=%h %s", "--stat", f"{before}..{after}"],
                             cwd=cwd, capture_output=True, text=True, timeout=15, check=False)
        return out.stdout.strip()[:12000] if out.returncode == 0 else f"Could not read commits: {out.stderr.strip()}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Could not read commits: {exc}"


def _brief(value: str) -> str:
    return " ".join(value.split())[:500]


def _task_line(lines: list[str], task: str) -> int:
    in_tasks = False
    hits = []
    for i, line in enumerate(lines):
        if line.startswith("## "):
            in_tasks = line.rstrip() in ("## Tasks", "## Lanes")
        if in_tasks and line.startswith("|") and not _is_separator(_cells(line)) and task in _cells(line):
            hits.append(i)
    if len(hits) != 1:
        raise ValueError("could not identify one task table row for reconciliation")
    return hits[0]


def _set_owner_state(line: str, old: re.Pattern, new: str, why: str) -> str:
    # Owner and state are adjacent in every supported tracker table. Replace only that
    # span; leave the item and every other cell byte-for-byte as approved.
    replaced, count = old.subn(lambda _: new, line)
    if count != 1:
        raise ValueError(why)
    return replaced


def _running(name: str) -> re.Pattern:
    return re.compile(rf"\|\s*{re.escape(name)}\s*\|\s*running\s+\d{{1,2}}:\d{{2}}\s*\|", re.I)


def _tree_note(root: Path, cwd: Path) -> str:
    """How the File ownership row names the tree: relative to the Worktrees dir, as audit reads it."""
    base = (root / worktrees_dir((root / "CLAUDE.md").read_text())).resolve()
    try:
        tree = str(cwd.resolve().relative_to(base))
    except ValueError:
        tree = str(cwd.resolve())
    branch = subprocess.run(["git", "-C", str(cwd), "branch", "--show-current"],
                            capture_output=True, text=True, check=False).stdout.strip()
    if any(c in tree + branch for c in "|`()\n"):
        raise ValueError(f"tree {tree!r} on {branch!r} cannot be written into a File ownership row")
    return f"worktree `{tree}` ({branch or 'detached'})"


def record_launch(path: Path, task: str, name: str, tree: str, runtime: str, model: str, at: str,
                  effort: str = "") -> None:
    """Before the worker runs: the row is the worker's and running, File ownership names its tree, and the Log says so."""
    if "|" in name or "\n" in name:
        raise ValueError("worker name cannot be written as a tracker owner")

    def change(text: str) -> str:
        lines = text.splitlines(keepends=True)
        i = _task_line(lines, task)
        dispatch_prompt.refuse_overlap(text, task)  # under the lock, so two launches that share a path cannot both pass
        lines[i] = _set_owner_state(lines[i], re.compile(r"\|\s*unassigned\s*\|\s*open\s*\|", re.I),
                                    f"| {name} | running {at} |", "task owner/state is not an unassigned open row")
        keys = dispatch_prompt.task_keys(task, dispatch_prompt.task_name(text, task))
        section = False
        owned = None
        for j, line in enumerate(lines):
            if line.startswith("## "):
                section = line.rstrip() == ownership.HEADING
            elif section and any(row.context in keys for row in ownership.parse(line)):
                owned = j
                break
        if owned is None:
            raise ValueError(f"no File ownership row for {task!r}")
        body = lines[owned].rstrip("\n")
        if body.lstrip().startswith("|"):
            if len(_cells(body)) != 2:
                raise ValueError("the File ownership row is not a two-cell `| context | paths |` row")
            cut = body.rstrip().rstrip("|").rstrip()
            lines[owned] = f"{cut}; {tree} |\n"
        else:
            lines[owned] = f"{body.rstrip()}; {tree}\n"
        return tracker_write.append_log("".join(lines), started_line(at, name, runtime, model, tree, task, effort), create=True)

    tracker_write.edit(path, change)


def update_tracker(path: Path, task: str, name: str, owner: str, status: str, reason: str, changes: str,
                   at: str) -> None:
    """Set the dispatched row to waiting, or back to ready on a relaunch, and append a durable local result note."""
    if "|" in owner or "\n" in owner:
        raise ValueError("configured user name cannot be written as a tracker owner")

    def change(text: str) -> str:
        lines = text.splitlines(keepends=True)
        i = _task_line(lines, task)
        running, why = _running(name), "task row changed during the one-shot run; tracker needs manual reconciliation"
        if status == "relaunch":
            # Back to unassigned and open: ready, so the coordinator dispatches it again.
            lines[i] = _set_owner_state(lines[i], running, "| unassigned | open |", why)
            note = relaunch_line(at, name, task, _brief(reason))
        else:
            lines[i] = _set_owner_state(lines[i], running, f"| {owner} | waiting |", why)
            note = ended_line(at, name, status, _brief(reason), _brief(changes))
        return tracker_write.append_log("".join(lines), note, create=True)

    tracker_write.edit(path, change)


def launcher_copy(root: Path, cwd: Path) -> Path:
    return root / dispatch_prompt.REPORTS_DIR / f"{cwd.name}.toon"


def write_report(root: Path, cwd: Path, report: dict) -> dict:
    """The launcher's own copy, which `result` reads, then the worker's in the tree; returns what was written. The text is cut
    to what `result` reads and written as UTF-8. A copy that cannot be written is an error in the report, and the old one goes
    so it is not served as this run's; the tree is the worker's, so a failure there costs nothing."""
    report = {**report, **{key: report[key][:FIELD_MAX] for key in ("reason", "changes")}}
    mine = launcher_copy(root, cwd)
    try:
        atomic_write(mine, (toon_encode(report) + "\n").encode("utf-8", errors="replace"))
    except OSError as exc:
        with suppress(OSError):
            mine.unlink(missing_ok=True)
        report["errors"] = [*report["errors"], f"report copy: {exc}"]
    data = (toon_encode(report) + "\n").encode("utf-8", errors="replace")
    if not (cwd / REPORT).parent.is_symlink():
        with suppress(OSError):
            atomic_write(cwd / REPORT, data)
    return report


def reconcile(root: Path, day: str, task: str, name: str, cwd: Path, exit_code: int,
              before_head: str | None = None, read_only: bool = False) -> dict:
    cfg = dispatch_prompt.config(root)
    settings = load_settings(root, cfg.settings_path)
    status, reason, changes = worker_result(cwd / RESULT, exit_code)
    actual = changed_files(cwd)
    commits = committed_changes(cwd, before_head)
    # The result lands on the day the run ends, falling back to the launch day's (#93).
    path = root / cfg.tracker_path(day)
    end = root / cfg.tracker_path(datetime.now(cfg.zone).date().isoformat())
    if end.exists() and any(row.item.strip() == task for row in parse_tracker(end.read_text()).tasks):
        path = end
    if status == "done" and read_only and (actual or commits != NO_COMMITS):
        # A task that owns nothing changed something: whatever the worker wrote, someone looks (#57).
        left = "\n".join(x for x in (actual, commits) if x and x != NO_COMMITS)
        status, reason = "human_review", f"read-only task left changes: {left}; worker said: {reason}"
    if status == "done" and actual:
        status = "human_review"
        reason = f"worker reported completion but the worktree is not clean: {actual}"
    if status == "relaunch":
        # A relaunch is for a run that changed nothing, once: anything else is someone's to look at.
        if actual or commits != NO_COMMITS:
            status, reason = "human_review", f"worker asked to be relaunched but left changes: {reason}"
        elif RELAUNCHED.format(task=task) in path.read_text():
            status, reason = "human_review", f"already relaunched once today and stopped again: {reason}"
    summary = (changes + f"\nCommits from this run:\n{commits}" +
               (f"\nWorking tree:\n{actual}" if actual else "\nWorking tree clean"))
    rows = [row for row in parse_tracker(path.read_text()).tasks if row.item.strip() == task]
    row = rows[0] if len(rows) == 1 else None
    errors = []
    if row is None or row.owner.strip().lower() != name.lower() or row.kind != "running":
        report = {"status": "human_review", "task": task, "worker": name, "runtime_exit": exit_code,
                  "reason": "task row changed during the one-shot run; reconcile it manually",
                  "changes": summary, "errors": [f"{dispatch_prompt.TRACKER_ERROR} task row changed; no issue or tracker update was made"]}
        return write_report(root, cwd, report)
    ref = backlog.issue_ref(row.issue, cfg.backlog) if row and row.issue.strip() else None
    if status == "human_review" and ref:
        if settings.kanban:
            error = kanban.add_human_hold(ref, cfg.backlog, settings.kanban)
            if error:
                errors.append(f"review hold: {error}")
        else:
            errors.append("review hold: workspace has no [kanban] configuration")
        home = (backlog.GitHubBacklog(ref.repo) if ref.host == backlog.GITHUB else
                cfg.backlog.sibling(ref.repo)
                if isinstance(cfg.backlog, backlog.Backlog) else None)
        if home is not None:
            body = f"One-shot worker {name} needs human review.\n\nWhy: {reason}\n\nChanges made:\n{summary}"
            commented = backlog.comment(home, ref.number, body, commit=True)
            if not commented.done:
                errors.append(f"issue comment: {commented.error}")
    try:
        update_tracker(path, task, name, cfg.user, status, reason, summary,
                       tracker_write.stamp(cfg.zone))
    except (OSError, ValueError) as exc:
        errors.append(f"{dispatch_prompt.TRACKER_ERROR} {exc}")
    report = {"status": status, "task": task, "worker": name, "runtime_exit": exit_code,
              "reason": reason, "changes": summary, "errors": errors}
    return write_report(root, cwd, report)


@contextmanager
def slot(trees: Path, cwd: Path, task: str, cap: int | None):
    """Claim a worker slot under the Worktrees dir's lock, so two sessions launching at once cannot both
    take the last one; release it when the run ends."""
    lock = os.open(trees, os.O_RDONLY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        busy = running_trees(trees)
        if cap is not None and len(busy) >= cap:
            raise ValueError(f"{len(busy)} one-shot workers running; [workers] max_concurrency is {cap} "
                             "(list them: chief-of-stuff processes --root <workspace>)")
        (cwd / PIDFILE).parent.mkdir(exist_ok=True)
        (cwd / PIDFILE).write_text(f"{os.getpid()} {task}\n")
    finally:
        os.close(lock)
    try:
        yield
    finally:
        (cwd / PIDFILE).unlink(missing_ok=True)


def run(*, root: Path, day: str | None, task: str, cwd: Path, name: str,
        runtime: str, agent_type: str | None, model: str, effort: str, dry_run: bool,
        timeout_minutes: int = 60) -> int:
    cfg = dispatch_prompt.config(root)
    chosen_day = day or datetime.now(cfg.zone).date().isoformat()
    body = dispatch_prompt.compose(root, chosen_day, task, worktree=cwd, name=name,
                                   runtime=runtime, one_shot=True)
    binary = resolve(runtimes.binary(runtime))
    if not binary:
        raise ValueError(f"{runtime} is unavailable in the configured interactive login shell")
    dispatch = cwd / dispatch_prompt.DISPATCH_FILE
    argv = command(runtime, binary, cwd, dispatch, agent_type=agent_type, model=model, effort=effort)
    if dry_run:
        print("would run one-shot: " + " ".join(shlex.quote(x) for x in argv))
        print(f"would write: {dispatch}")
        return 0
    if not cwd.is_dir():
        raise ValueError(f"no worktree at {cwd}")
    trees = root / worktrees_dir((root / "CLAUDE.md").read_text())
    with slot(trees, cwd, task, load_settings(root, cfg.settings_path).workers.max_concurrency):
        return _launch(root, cfg, chosen_day, task, cwd, name, runtime, model, effort, body, argv, timeout_minutes)


def _launch(root: Path, cfg, chosen_day: str, task: str, cwd: Path, name: str, runtime: str, model: str,
            effort: str, body: str, argv: list[str], timeout_minutes: int) -> int:
    # Same clean login-shell path as interactive sessions; auth and user PATH come from shell setup.
    # Everything that can refuse runs before anything is written; a refused row takes its dispatch back.
    launch_argv, tree = login_argv(argv), _tree_note(root, cwd)
    written = dispatch_prompt.write_dispatch(cwd, body)
    logs = cwd / dispatch_prompt.PROMPT_DIR
    before_head = git_head(cwd)
    try:
        record_launch(root / cfg.tracker_path(chosen_day), task, name, tree, runtime, model,
                      tracker_write.stamp(cfg.zone), effort)
    except Exception:
        written.unlink(missing_ok=True)
        raise
    # An earlier run's copy must not read as this run's if the launcher dies before reconciling.
    with suppress(OSError):  # something in the way of the reports folder is no reason to leave the row running
        launcher_copy(root, cwd).unlink(missing_ok=True)
    # A relaunch in a fresh tree leaves the old tree's copy; `result` would pick it by mtime. Other tasks' copies stay.
    for copy in (root / dispatch_prompt.REPORTS_DIR).glob("*.toon"):
        try:
            if read_report(copy)["task"] == task:
                copy.unlink()
        except UNREADABLE:
            pass
    env = clean_env()
    env["CHIEF_OF_STUFF_WORKSPACE"] = str(root.resolve())
    try:
        with (logs / "worker-stdout.log").open("w") as stdout, (logs / "worker-stderr.log").open("w") as stderr:
            completed = subprocess.run(launch_argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
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
    report = reconcile(root, chosen_day, task, name, cwd, exit_code, before_head,
                       read_only=dispatch_prompt.READ_ONLY in body.splitlines())  # held to what it was told
    print(toon_encode(report))
    return 0 if report["status"] == "done" and not report["errors"] else 1
