#!/usr/bin/env python3
"""Install a pinned chief-of-stuff release and run it in an agent CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import date
from pathlib import Path

from scripts._vendor.toon_format import ToonDecodeError, decode as toon_decode

SOURCE = Path(__file__).resolve().parent
BINARIES = {"claude": "claude", "codex": "codex", "cursor": "agent", "agy": "agy"}
WATCH_INTERVAL = 5 * 60


class Refused(ValueError):
    pass


def files(source: Path) -> list[Path]:
    includes = [source / "agents", source / "scripts", source / "assets", source / ".claude-plugin"]
    result = [source / "start_coordinator.py"]
    for folder in includes:
        result.extend(p for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts
                      and p.suffix != ".pyc")
    return sorted(result)


def install(source: Path, install_dir: Path) -> Path:
    manifest = source / ".claude-plugin" / "plugin.json"
    version = json.loads(manifest.read_text())["version"]
    digest = hashlib.sha256()
    selected = files(source)
    for path in selected:
        digest.update(str(path.relative_to(source)).encode() + b"\0" + path.read_bytes())
    target = install_dir / "versions" / f"{version}-{digest.hexdigest()[:12]}"
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".chief-install-", dir=target.parent) as stage_name:
        stage = Path(stage_name)
        for path in selected:
            output = stage / path.relative_to(source)
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, output)
        try:
            stage.rename(target)
        except OSError:
            if not target.is_dir():
                raise
    return target


def prompt(runtime: str, release: Path, root: Path) -> str:
    rules = (release / "agents" / "chief-of-stuff.md").read_text()
    rules = rules.split("---", 2)[-1].strip()
    rules = rules.replace("${CLAUDE_PLUGIN_ROOT}", str(release))
    if runtime != "claude":
        mailbox_check = (f'python3 "{release / "scripts" / "inbox.py"}" '
                         f'--mailbox-dir "{root / ".chief-of-stuff" / "mailbox"}" '
                         'list --recipient coordinator --unread')
        host_schedule = {
            "agy": ("After opening the day, create one Antigravity `Schedule` at `*/5 * * * *` "
                    f"to run `{mailbox_check}` and process unread worker messages as a normal "
                    "coordinator turn. The CLI `/schedule \"*/5 * * * *\" ...` is the equivalent "
                    "when the Schedule tool is unavailable. Avoid duplicates and cancel a "
                    "persistent schedule when this session ends."),
            "cursor": ("After opening the day, start one Cursor in-session `/loop 5m` to run "
                       f"`{mailbox_check}` and process unread worker messages as a normal "
                       "coordinator turn. Stop the loop when this session ends. If `/loop` is "
                       "unavailable, rely on the watcher and check at the start of each turn."),
            "codex": ("Codex CLI has no in-session schedule. The session-scoped watcher checks "
                      "every five minutes and notifies on new messages. At the start of every "
                      "turn, read the inbox and reconcile the tracker and board; do not claim "
                      "that a notification woke this conversation."),
        }[runtime]
        rules = rules.replace("`CronList`, then `CronCreate` only when no job's prompt starts `[Scheduled check]` (see Check)",
                              "confirm the session-scoped watcher (see Check)")
        rules = rules.replace("list sessions;", "inspect registered workers;")
        rules = rules.replace("list sessions,", "inspect registered workers,")
        check = ("## Check\n\nA session-scoped watcher checks the inbox and task audit every five minutes "
                 "while this CLI session is open. It sends notifications for new findings and never "
                 "edits the tracker or board. On the next coordinator turn, read the clock, inbox, "
                 "audit and worker registry; reconcile task state, then render the board. "
                 "Do not launch a new session without the user's explicit yes.\n\n" + host_schedule + "\n\n")
        rules = re.sub(r"## Check\n.*?(?=## Sessions\n)", check, rules, flags=re.S)
        sessions = ("## Sessions\n\nThe workspace `Sessions:` line may name Claude native list and send tools. "
                    "Use those only when your host exposes them. For every non-Claude worker, "
                    "run the pinned `scripts/process_status.py --root <workspace>` to check "
                    "registered worker PIDs in one batch. The assigned name, worktree and process "
                    "identify it; do not invent a Claude ref. Receive registrations and reports "
                    "through the shared mailbox and send replies there. A missing registration "
                    "is `starting`; a dead registered process can orphan its task after audit. "
                    "Preserve the tracker `## Sessions` states: planning, working, waiting, idle.\n\n")
        rules = re.sub(r"## Sessions\n.*?(?=## Relay\n)", sessions, rules, flags=re.S)
        rules += ("\n\n## Host adapter\n\nYou run in " + runtime + ". Use your host's own tools to read, edit and run the pinned scripts above. "
                  "The `Sessions:` line in CLAUDE.md describes Claude's peer tools; you do not have those tools. "
                  "Check non-Claude workers with the pinned `scripts/process_status.py --root <workspace>` "
                  "batch helper; use the shared `scripts/inbox.py` "
                  "mailbox for their messages. Claude workers retain their native session tools when available. "
                  "A five-minute watcher audits tasks and unread inbox messages while this session "
                  "is open and sends notifications. "
                  "On your next turn, reconcile the tracker and board. Never launch a new session without "
                  "the user's explicit approval. If the named calendar tool is unavailable, "
                  "report it, leave calendar facts unverified, and continue work that does not depend on it.\n")
    return (f"You are chief-of-stuff. The installed rules and scripts are pinned at {release}. "
            f"The workspace is {root}. Read its CLAUDE.md for configuration.\n\n{rules}\n\nOpen the day.")


def command(runtime: str, binary: str, release: Path, root: Path) -> list[str]:
    if runtime == "claude":
        return [binary, "--plugin-dir", str(release), "--agent", "chief-of-stuff", "Open the day."]
    initial = prompt(runtime, release, root)
    if runtime == "codex":
        return [binary, "-C", str(root), initial]
    if runtime == "cursor":
        return [binary, "--workspace", str(root), initial]
    return [binary, "-i", initial]


def check_once(root: Path, release: Path) -> str:
    scripts = release / "scripts"
    mailbox = root / ".chief-of-stuff" / "mailbox"
    inbox = subprocess.run([sys.executable, str(scripts / "inbox.py"), "--mailbox-dir", str(mailbox),
                            "list", "--recipient", "coordinator", "--unread"], capture_output=True,
                           text=True, timeout=30)
    audit = subprocess.run([sys.executable, str(scripts / "audit_tasks.py"), "--date", date.today().isoformat()],
                           cwd=root, capture_output=True, text=True, timeout=60)
    workers = subprocess.run([sys.executable, str(scripts / "process_status.py"), "--root", str(root)],
                             capture_output=True, text=True, timeout=10)
    if inbox.returncode or audit.returncode:
        return f"inbox exit {inbox.returncode}; audit exit {audit.returncode}"
    lines = []
    try:
        unread = [] if inbox.stdout.strip() == "[]" else toon_decode(inbox.stdout)
        if isinstance(unread, list) and unread:
            lines.append(f"{len(unread)} unread coordinator message(s)")
    except (ValueError, ToonDecodeError):
        lines.append("coordinator inbox could not be parsed")
    lines.extend(line for line in audit.stdout.splitlines()
                 if re.match(r"(?i)^(orphaned work:|stopped:|reopen:|issue:|next decision:|overdue:|waiting:)", line))
    if workers.returncode:
        lines.append("worker process check failed")
    else:
        try:
            statuses = [] if workers.stdout.strip() == "[]" else toon_decode(workers.stdout)
            for entry in statuses:
                if entry["status"] in ("gone", "pid_reused"):
                    lines.append(f"worker {entry['name']} process is {entry['status']}")
        except (ValueError, TypeError, KeyError, ToonDecodeError):
            lines.append("worker process check could not be parsed")
    return " | ".join(lines[:5])[:400]


def watcher(root: Path, release: Path, stop: threading.Event, interval: float = WATCH_INTERVAL) -> None:
    previous = ""
    while not stop.wait(interval):
        try:
            summary = check_once(root, release)
            if summary and summary != previous:
                subprocess.run([sys.executable, str(release / "scripts" / "notify.py"), "--root", str(root),
                                "add", "--kind", "awaiting", "--what", "Coordinator check",
                                "--message", summary], capture_output=True, text=True, timeout=30)
            previous = summary
        except (OSError, subprocess.TimeoutExpired):
            continue


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runtime", choices=BINARIES, required=True)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--install-dir", type=Path, default=Path.home() / ".local" / "share" / "chief-of-stuff")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    binary = shutil.which(BINARIES[args.runtime])
    try:
        if not binary:
            raise Refused(f"{BINARIES[args.runtime]} is unavailable on PATH")
        if not root.is_dir():
            raise Refused(f"workspace {root} does not exist")
        release = install(SOURCE, args.install_dir.expanduser().resolve())
        cmd = command(args.runtime, binary, release, root)
    except (Refused, OSError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(json.dumps({"release": str(release), "runtime": args.runtime, "argv": cmd}))
        return 0
    stop = threading.Event()
    monitor = None
    if args.runtime != "claude":
        monitor = threading.Thread(target=watcher, args=(root, release, stop), daemon=True)
        monitor.start()
    try:
        return subprocess.call(cmd, cwd=root)
    finally:
        stop.set()
        if monitor:
            monitor.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
