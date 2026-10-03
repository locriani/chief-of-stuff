"""Independent notification reconciliation and macOS user LaunchAgent lifecycle."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import plistlib
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from notify import configuration, sync_workspace
from render_board import ConfigError, daily_trackers
from settings import SettingsError

POLL = 5
RECONCILE = 60
RELEASE = Path(__file__).resolve().parent.parent


class ServiceError(RuntimeError):
    pass


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def state_dir(root: Path) -> Path:
    return root.resolve() / ".chief-of-stuff"


@contextmanager
def instance_lock(root: Path, name: str = "notify-service.lock"):
    directory = state_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / name).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ServiceError("notification service already running or being managed") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def fingerprints(root: Path, now: datetime) -> tuple:
    cfg, _ = configuration(root)
    day = now.astimezone(cfg.zone).date()
    paths = {root / "CLAUDE.md", root / cfg.tracker_path(day.isoformat())}
    if cfg.settings_path:
        paths.add(root / cfg.settings_path)
    paths.update(path for tracker_day, path in daily_trackers(root, cfg) if tracker_day <= day)
    return (day.isoformat(), tuple((str(path), hashlib.sha256(path.read_bytes()).hexdigest()
                                   if path.is_file() else None) for path in sorted(paths)))


class Service:
    def __init__(self, root: Path, reconcile=sync_workspace):
        self.root = root.resolve()
        self.reconcile = reconcile
        self.inputs = None
        self.last_sync = None
        self.last_wall = None
        self.last_tick = None
        self.record = {"pid": os.getpid(), "release": str(RELEASE),
                       "last_success": None, "error": None}

    def tick(self, now: datetime, elapsed: float):
        # A wall/monotonic discrepancy detects sleep and clock adjustments on hosts
        # whose monotonic clock excludes suspended time.
        wall = now.timestamp()
        jumped = (self.last_wall is not None and
                  abs((wall - self.last_wall) - (elapsed - self.last_tick)) >= POLL)
        self.last_wall, self.last_tick = wall, elapsed
        try:
            current = fingerprints(self.root, now)
            if (current == self.inputs and self.record["error"] is None and not jumped
                    and self.last_sync is not None and elapsed - self.last_sync < RECONCILE):
                return
            changes = self.reconcile(self.root, now)
            self.inputs, self.last_sync = current, elapsed
            self.record.update(last_success=now.isoformat(), error=None,
                               disabled=changes.lines == ["notify: off"])
            for line in changes.lines:
                if line != "notify: off":
                    print(line, flush=True)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if message != self.record["error"]:
                print(f"notify service: {message}", file=sys.stderr, flush=True)
            self.record["error"] = message
        atomic_write(state_dir(self.root) / "notify-status.json",
                     json.dumps(self.record).encode())


def serve(root: Path):
    stop_event = threading.Event()
    with instance_lock(root):
        previous = {sig: signal.signal(sig, lambda *_: stop_event.set())
                    for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            worker = Service(root)
            while not stop_event.is_set():
                worker.tick(datetime.now(timezone.utc), time.monotonic())
                stop_event.wait(POLL)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def status(root: Path) -> dict:
    path = state_dir(root) / "notify-status.json"
    record = json.loads(path.read_text()) if path.exists() else {
        "release": None, "last_success": None, "error": None}
    lock = state_dir(root) / "notify-service.lock"
    running = False
    if lock.exists():
        with lock.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                running = True
    return dict(record, running=running)


def label(root: Path) -> str:
    key = hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:20]
    return f"com.chief-of-stuff.notify.{key}"


def launchctl(*args: str, missing_ok: bool = False) -> bool:
    result = subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=15)
    if result.returncode:
        if missing_ok and any(word in result.stderr.lower() for word in
                              ("could not find service", "no such process", "service not found")):
            return False
        raise ServiceError(result.stderr.strip() or f"launchctl exited {result.returncode}")
    return True


def job_path(root: Path, jobs_dir: Path | None) -> Path:
    return (jobs_dir or Path.home() / "Library/LaunchAgents") / f"{label(root)}.plist"


def require_macos():
    if sys.platform != "darwin":
        raise ServiceError("managed notifications require macOS; run notify serve under your host supervisor")


def ensure(root: Path, jobs_dir: Path | None = None) -> str:
    root = root.resolve()
    _, settings = configuration(root)
    if settings.notify.adapter == "off":
        return "notify: off"
    require_macos()
    with instance_lock(root, "notify-management.lock"):
        name = label(root)
        target = f"gui/{os.getuid()}/{name}"
        directory = state_dir(root)
        path = job_path(root, jobs_dir)
        desired = {"Label": name,
                   "ProgramArguments": [str(Path(sys.executable).resolve()),
                                        str(RELEASE / "scripts/notify.py"), "--root", str(root), "serve"],
                   "WorkingDirectory": str(root), "RunAtLoad": True, "KeepAlive": True,
                   "ThrottleInterval": 10,
                   "StandardOutPath": str(directory / "notify-service.log"),
                   "StandardErrorPath": str(directory / "notify-service.log")}
        existing = plistlib.loads(path.read_bytes()) if path.exists() else None
        if existing is not None and (existing.get("Label") != name or
                                     existing.get("WorkingDirectory") != str(root)):
            raise ServiceError(f"refusing to replace unrelated LaunchAgent: {path}")
        loaded = launchctl("print", target, missing_ok=True)
        if existing == desired and loaded:
            return "notify: serving"
        if loaded:
            launchctl("bootout", target)
        atomic_write(path, plistlib.dumps(desired))
        # stop removes the plist; enable also recovers a job disabled externally.
        launchctl("enable", target)
        launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
        return "notify: restarted" if existing or loaded else "notify: started"


def stop(root: Path, jobs_dir: Path | None = None) -> str:
    require_macos()
    with instance_lock(root, "notify-management.lock"):
        path = job_path(root, jobs_dir)
        target = f"gui/{os.getuid()}/{label(root)}"
        if path.exists():
            job = plistlib.loads(path.read_bytes())
            if job.get("Label") != label(root) or job.get("WorkingDirectory") != str(root.resolve()):
                raise ServiceError(f"refusing to remove unrelated LaunchAgent: {path}")
        # Remove the login registration first so an interrupted stop cannot leave
        # a job which reappears at the next login.
        path.unlink(missing_ok=True)
        if launchctl("print", target, missing_ok=True):
            launchctl("bootout", target)
        return "notify: stopped"


def main(command: str, root: Path) -> int:
    try:
        if command == "serve":
            serve(root)
        elif command == "status":
            from _vendor.toon_format import encode
            print(encode(status(root)))
        else:
            print(ensure(root) if command == "ensure" else stop(root))
        return 0
    except (OSError, ValueError, RuntimeError, ConfigError, SettingsError, subprocess.TimeoutExpired) as exc:
        print(f"notify: {exc}", file=sys.stderr)
        return 2
