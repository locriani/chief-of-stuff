#!/usr/bin/env python3
"""File-backed bidirectional mailbox system for chief-of-stuff.

Provides reliable, discrete, lock-free file-backed messaging between the coordinator
session and spawned subagent / worker sessions without relying on IPC or MCP tools.

File layout:
<mailbox_root>/
├── .gitignore                     # Ignores all message files (*\\n!.gitignore)
├── .tmp/                          # Filesystem-local staging area for atomic writes
│   └── write_<pid>_<uuid>.tmp
└── <recipient>/                   # Per-recipient mailbox (e.g. 'coordinator')
    ├── incoming/                  # Unread immutable message files
    │   └── msg_<timestamp>_<uuid>.json
    ├── read/                      # Acknowledged/archived message files
    │   └── msg_<timestamp>_<uuid>.json
    └── dead-letter/               # Quarantined malformed or unparseable files
        └── msg_<timestamp>_<uuid>.json
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Sequence
import uuid

# Safe filesystem name regex
RE_UNSAFE_CHARS = re.compile(r"[^a-zA-Z0-9._-]")
RE_SAFE_MESSAGE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,255}$")

# Valid message types
VALID_MESSAGE_TYPES = {
    "register",
    "registration",
    "progress",
    "report",
    "ask",
    "reply",
    "stop",
    "review",
    "generic",
    "general",
}

# Subdirectory names
DIR_INCOMING = "incoming"
DIR_READ = "read"
DIR_DEAD_LETTER = "dead-letter"
DIR_TMP = ".tmp"

# Environment variable for custom mailbox root
ENV_MAILBOX_DIR = "CHIEF_MAILBOX_DIR"


# ---------------------------------------------------------------------------
# Domain Exceptions (Subclassing ValueError per repository requirements)
# ---------------------------------------------------------------------------

class InboxError(ValueError):
    """Base exception for all mailbox domain operations."""


class MessageNotFoundError(InboxError):
    """Raised when a requested message ID does not exist in incoming or read."""


class CorruptedMessageError(InboxError):
    """Raised when a message file is unparseable, malformed, or missing required schema."""


class InvalidMessageError(InboxError):
    """Raised when message fields, parameters, or configurations are invalid."""


# ---------------------------------------------------------------------------
# Message Dataclass & Serialization
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Message:
    message_id: str
    timestamp: str
    sender: str
    recipient: str
    type: str
    body: str
    payload: dict[str, Any]
    task: str | None = None
    worktree: str | None = None
    headers: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "timestamp": self.timestamp,
            "sender": self.sender,
            "recipient": self.recipient,
            "type": self.type,
            "body": self.body,
            "payload": self.payload,
            "task": self.task,
            "worktree": self.worktree,
            "headers": self.headers,
        }

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        if not isinstance(data, dict):
            raise CorruptedMessageError(f"Message data must be a dict, got {type(data).__name__}")

        required = ("message_id", "timestamp", "sender", "recipient", "type")
        missing = [k for k in required if k not in data or not str(data[k]).strip()]
        if missing:
            raise CorruptedMessageError(f"Message data missing required fields: {', '.join(missing)}")

        payload = data.get("payload")
        if payload is None:
            payload = {}
        elif not isinstance(payload, dict):
            raise CorruptedMessageError("Message payload must be a dict or null")
        else:
            payload = dict(payload)

        body = data.get("body")
        if body is None:
            body = payload.get("body", "") if isinstance(payload, dict) else ""
        if not isinstance(body, str):
            body = str(body)

        headers = data.get("headers")
        if headers is None:
            headers = {}
        elif not isinstance(headers, dict):
            raise CorruptedMessageError("Message headers must be a dict or null")
        else:
            headers = dict(headers)

        task = data.get("task", data.get("lane"))  # `lane` in a message stored before #33
        if task is not None:
            task = str(task)

        worktree = data.get("worktree")
        if worktree is not None:
            worktree = str(worktree)

        return cls(
            message_id=str(data["message_id"]),
            timestamp=str(data["timestamp"]),
            sender=str(data["sender"]),
            recipient=str(data["recipient"]),
            type=str(data["type"]),
            body=body,
            payload=payload,
            task=task,
            worktree=worktree,
            headers=headers,
        )

    @classmethod
    def from_json(cls, raw: str) -> Message:
        if not raw or not raw.strip():
            raise CorruptedMessageError("Message JSON content is empty")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CorruptedMessageError(f"Invalid JSON: {exc}") from exc
        return cls.from_dict(data)

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)

    def keys(self) -> list[str]:
        return list(self.to_dict().keys())

    def values(self) -> list[Any]:
        return list(self.to_dict().values())

    def items(self) -> list[tuple[str, Any]]:
        return list(self.to_dict().items())


# ---------------------------------------------------------------------------
# Storage Layout & Sanitization Helpers
# ---------------------------------------------------------------------------

def sanitize_recipient(recipient: str) -> str:
    """Sanitize recipient identifier into a safe filesystem directory name."""
    if not recipient or not recipient.strip():
        raise InvalidMessageError("Recipient cannot be empty or whitespace only")
    cleaned = recipient.strip()
    if ".." in cleaned or "/" in cleaned or "\\" in cleaned or "\x00" in cleaned:
        raise InvalidMessageError(f"Recipient contains invalid path characters: {recipient!r}")

    if cleaned.lower() == "coordinator":
        return "coordinator"

    sanitized = RE_UNSAFE_CHARS.sub("_", cleaned)
    while sanitized.startswith("."):
        sanitized = "_" + sanitized[1:]
    if not sanitized or sanitized.replace("_", "") == "":
        raise InvalidMessageError(f"Recipient results in invalid filesystem name: {recipient!r}")
    return sanitized


def validate_sender(sender: str) -> str:
    """Validate sender string."""
    if not sender or not sender.strip():
        raise InvalidMessageError("Sender cannot be empty or whitespace only")
    return sender.strip()


def validate_message_id(message_id: str) -> str:
    """Validate and normalize a message ID for filesystem lookup."""
    if not message_id or not isinstance(message_id, str):
        raise InvalidMessageError("message_id must be a non-empty string")
    clean = message_id.strip()
    if clean.endswith(".json"):
        clean = clean[:-5]
    if ".." in clean or "/" in clean or "\\" in clean or "\x00" in clean:
        raise InvalidMessageError(f"Path traversal detected in message_id: {message_id!r}")
    if not RE_SAFE_MESSAGE_ID.match(clean):
        raise InvalidMessageError(f"Invalid characters in message_id: {message_id!r}")
    return clean


def generate_message_id() -> tuple[str, str]:
    """Generate sortable UTC message ID matching standard pattern and ISO-8601 timestamp."""
    now = datetime.now(timezone.utc)
    ts_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    ts_compact = now.strftime("%Y%m%dT%H%M%S%fZ")
    nonce = uuid.uuid4().hex[:12]
    msg_id = f"msg_{ts_compact}_{nonce}"
    return msg_id, ts_iso


def resolve_mailbox_root(mailbox_dir: Path | str | None = None) -> Path:
    """Locate or default the mailbox storage root directory.

    Priority:
    1. Explicit mailbox_dir argument (or --mailbox-dir CLI flag)
    2. CHIEF_MAILBOX_DIR environment variable
    3. Git common dir lookup (for worktrees sharing repository mailbox)
    4. Upward filesystem search from cwd for .chief-of-stuff/mailbox or CLAUDE.md
    5. Default to <cwd>/.chief-of-stuff/mailbox
    """
    root: Path | None = None

    if mailbox_dir is not None and str(mailbox_dir).strip():
        root = Path(mailbox_dir).resolve()
    elif os.environ.get(ENV_MAILBOX_DIR):
        root = Path(os.environ[ENV_MAILBOX_DIR]).resolve()
    else:
        # Check git common dir
        try:
            res = subprocess.run(
                ["git", "rev-parse", "--git-common-dir"],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0:
                git_common = res.stdout.strip()
                if git_common:
                    common_path = Path(git_common)
                    if not common_path.is_absolute():
                        common_path = (Path.cwd() / common_path).resolve()
                    if common_path.name == ".git":
                        root = common_path.parent / ".chief-of-stuff" / "mailbox"
                    elif common_path.parent.name == ".git":
                        root = common_path.parent.parent / ".chief-of-stuff" / "mailbox"
        except Exception:
            pass

        if root is None:
            curr = Path.cwd().resolve()
            candidates = [curr, *curr.parents]
            for p in candidates:
                if (p / ".chief-of-stuff" / "mailbox").exists() or (p / "CLAUDE.md").exists():
                    root = p / ".chief-of-stuff" / "mailbox"
                    break

        if root is None:
            root = Path.cwd().resolve() / ".chief-of-stuff" / "mailbox"

    root.mkdir(parents=True, exist_ok=True)
    (root / DIR_TMP).mkdir(parents=True, exist_ok=True)

    # Automatically write .gitignore (*\n!.gitignore\n)
    gitignore = root / ".gitignore"
    expected_ignore = "*\n!.gitignore\n"
    if not gitignore.exists():
        try:
            gitignore.write_text(expected_ignore, encoding="utf-8")
        except OSError:
            pass

    return root


def get_recipient_dirs(mailbox_root: Path, recipient: str) -> tuple[Path, Path, Path, Path]:
    """Ensure and return (recipient_dir, incoming_dir, read_dir, dead_letter_dir)."""
    sanitized = sanitize_recipient(recipient)
    recip_dir = mailbox_root / sanitized
    incoming_dir = recip_dir / DIR_INCOMING
    read_dir = recip_dir / DIR_READ
    dead_letter_dir = recip_dir / DIR_DEAD_LETTER

    incoming_dir.mkdir(parents=True, exist_ok=True)
    read_dir.mkdir(parents=True, exist_ok=True)
    dead_letter_dir.mkdir(parents=True, exist_ok=True)

    # Maintain local .tmp inside recipient as well
    (recip_dir / DIR_TMP).mkdir(parents=True, exist_ok=True)

    # Convenience symlinks for alternative terminology (unread -> incoming, archive -> read, corrupt -> dead-letter)
    for alias, target in [("unread", DIR_INCOMING), ("archive", DIR_READ), ("corrupt", DIR_DEAD_LETTER)]:
        link_path = recip_dir / alias
        if not link_path.exists() and not link_path.is_symlink():
            try:
                link_path.symlink_to(target)
            except OSError:
                pass

    return recip_dir, incoming_dir, read_dir, dead_letter_dir


# ---------------------------------------------------------------------------
# Atomic File Writing & Quarantine Helpers
# ---------------------------------------------------------------------------

def _atomic_write_json(mailbox_root: Path, target_file: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON data to target_file via staging in .tmp/ on the same filesystem."""
    tmp_dir = mailbox_root / DIR_TMP
    tmp_dir.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    uid = uuid.uuid4().hex
    temp_file = tmp_dir / f"write_{pid}_{uid}.tmp"
    json_text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            f.write(json_text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, target_file)
    except Exception:
        if temp_file.exists():
            try:
                temp_file.unlink()
            except OSError:
                pass
        raise


def _quarantine_file(file_path: Path, dead_letter_dir: Path) -> Path:
    """Atomically quarantine a malformed or corrupted file to dead-letter/."""
    dead_letter_dir.mkdir(parents=True, exist_ok=True)
    target = dead_letter_dir / file_path.name
    if target.exists():
        uid = uuid.uuid4().hex[:8]
        target = dead_letter_dir / f"corrupted_{uid}_{file_path.name}"
    try:
        os.replace(file_path, target)
    except FileNotFoundError:
        pass
    except Exception as exc:
        sys.stderr.write(f"Warning: Failed to quarantine {file_path.name}: {exc}\n")
    return target


def _parse_or_quarantine_file(
    file_path: Path,
    dead_letter_dir: Path,
    recipient: str,
) -> Message | None:
    """Read and validate a message file; quarantine to dead-letter/ if unparseable or corrupted."""
    try:
        raw = file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except Exception as exc:
        _quarantine_file(file_path, dead_letter_dir)
        sys.stderr.write(f"Quarantined unreadable message {file_path.name}: {exc}\n")
        return None

    if not raw.strip():
        _quarantine_file(file_path, dead_letter_dir)
        sys.stderr.write(f"Quarantined 0-byte message {file_path.name}\n")
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _quarantine_file(file_path, dead_letter_dir)
        sys.stderr.write(f"Quarantined invalid JSON message {file_path.name}: {exc}\n")
        return None

    try:
        return Message.from_dict(data)
    except CorruptedMessageError as exc:
        _quarantine_file(file_path, dead_letter_dir)
        sys.stderr.write(f"Quarantined malformed message {file_path.name}: {exc}\n")
        return None


# ---------------------------------------------------------------------------
# Programmatic Python API
# ---------------------------------------------------------------------------

def send_message(
    *args: Any,
    recipient: str | None = None,
    sender: str | None = None,
    body: str = "",
    msg_type: str = "generic",
    payload: dict[str, Any] | None = None,
    task: str | None = None,
    worktree: str | Path | None = None,
    headers: dict[str, Any] | None = None,
    mailbox_dir: Path | str | None = None,
    **kwargs: Any,
) -> Message:
    """Atomically deposit an immutable message into a recipient's incoming inbox."""
    # Handle kwargs aliases
    if "type" in kwargs and (msg_type == "generic" or not msg_type):
        msg_type = kwargs.pop("type")
    if "to" in kwargs and recipient is None:
        recipient = kwargs.pop("to")
    if "from" in kwargs and sender is None:
        sender = kwargs.pop("from")
    if "from_sender" in kwargs and sender is None:
        sender = kwargs.pop("from_sender")
    if "root" in kwargs and mailbox_dir is None:
        mailbox_dir = kwargs.pop("root")

    # Handle positional arguments
    pos_args = list(args)
    if pos_args:
        first = pos_args[0]
        if isinstance(first, Path) or (isinstance(first, str) and ("/" in first or "\\" in first)):
            if mailbox_dir is None:
                mailbox_dir = first
            pos_args = pos_args[1:]

    if pos_args and recipient is None:
        recipient = pos_args.pop(0)
    if pos_args and sender is None:
        sender = pos_args.pop(0)
    if pos_args and not body:
        body = pos_args.pop(0)
    if pos_args:
        msg_type = pos_args.pop(0)
    if pos_args and payload is None:
        payload = pos_args.pop(0)

    # Validations
    if recipient is None or not str(recipient).strip():
        raise InvalidMessageError("Recipient cannot be empty")
    clean_recipient = sanitize_recipient(str(recipient))

    if sender is None or not str(sender).strip():
        raise InvalidMessageError("Sender cannot be empty")
    clean_sender = validate_sender(str(sender))

    clean_type = str(msg_type).strip().lower()
    if clean_type not in VALID_MESSAGE_TYPES:
        raise InvalidMessageError(
            f"Invalid message type '{msg_type}'. Expected one of: {sorted(VALID_MESSAGE_TYPES)}"
        )

    if payload is None:
        if not body or not body.strip():
            raise InvalidMessageError("Message body or payload must be provided (cannot be empty)")
        clean_payload = {"body": body}
    elif not isinstance(payload, dict):
        raise InvalidMessageError(f"Payload must be a dict, got {type(payload).__name__}")
    else:
        clean_payload = dict(payload)
        if not body and "body" in clean_payload and isinstance(clean_payload["body"], str):
            body = clean_payload["body"]
        elif not body:
            body = json.dumps(clean_payload, ensure_ascii=False)

    if headers is None:
        clean_headers = {}
    elif not isinstance(headers, dict):
        raise InvalidMessageError("Headers must be a dict")
    else:
        clean_headers = dict(headers)

    clean_worktree = str(worktree) if worktree is not None else None
    clean_task = str(task) if task is not None else None

    # Construct message
    msg_id, ts_iso = generate_message_id()
    msg = Message(
        message_id=msg_id,
        timestamp=ts_iso,
        sender=clean_sender,
        recipient=clean_recipient,
        type=clean_type,
        body=body,
        payload=clean_payload,
        task=clean_task,
        worktree=clean_worktree,
        headers=clean_headers,
    )

    # Atomic write to disk
    root = resolve_mailbox_root(mailbox_dir)
    _, incoming_dir, _, _ = get_recipient_dirs(root, clean_recipient)
    target_file = incoming_dir / f"{msg_id}.json"
    _atomic_write_json(root, target_file, msg.to_dict())

    return msg


def list_messages(
    *args: Any,
    recipient: str | None = None,
    unread_only: bool = True,
    include_dead_letter: bool = False,
    mailbox_dir: Path | str | None = None,
    **kwargs: Any,
) -> list[Message]:
    """List messages for recipient ordered chronologically."""
    if "to" in kwargs and recipient is None:
        recipient = kwargs.pop("to")
    if "root" in kwargs and mailbox_dir is None:
        mailbox_dir = kwargs.pop("root")

    pos_args = list(args)
    if pos_args:
        first = pos_args[0]
        if isinstance(first, Path) or (isinstance(first, str) and ("/" in first or "\\" in first)):
            if mailbox_dir is None:
                mailbox_dir = first
            pos_args = pos_args[1:]

    if pos_args and recipient is None:
        recipient = pos_args.pop(0)
    if pos_args:
        unread_only = bool(pos_args.pop(0))
    if pos_args:
        include_dead_letter = bool(pos_args.pop(0))

    if recipient is None or not str(recipient).strip():
        raise InvalidMessageError("Recipient cannot be empty")
    clean_recip = sanitize_recipient(str(recipient))

    root = resolve_mailbox_root(mailbox_dir)
    recip_dir = root / clean_recip
    if not recip_dir.exists():
        return []

    incoming_dir = recip_dir / DIR_INCOMING
    read_dir = recip_dir / DIR_READ
    dead_letter_dir = recip_dir / DIR_DEAD_LETTER

    messages: list[Message] = []

    # 1. Scan incoming
    if incoming_dir.exists():
        files = sorted([f for f in incoming_dir.iterdir() if f.is_file()], key=lambda p: p.name)
        for f in files:
            msg = _parse_or_quarantine_file(f, dead_letter_dir, clean_recip)
            if msg is not None:
                messages.append(msg)

    # 2. Scan read if requested
    if not unread_only and read_dir.exists():
        files = sorted([f for f in read_dir.iterdir() if f.is_file()], key=lambda p: p.name)
        for f in files:
            try:
                raw = f.read_text(encoding="utf-8")
                messages.append(Message.from_json(raw))
            except Exception:
                pass

    # 3. Scan dead-letter if requested
    if include_dead_letter and dead_letter_dir.exists():
        files = sorted([f for f in dead_letter_dir.iterdir() if f.is_file()], key=lambda p: p.name)
        for f in files:
            try:
                raw = f.read_text(encoding="utf-8")
                messages.append(Message.from_json(raw))
            except Exception as exc:
                messages.append(
                    Message(
                        message_id=f.stem,
                        timestamp=datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%S.%fZ"
                        ),
                        sender="unknown",
                        recipient=clean_recip,
                        type="corrupted",
                        body=f"Corrupted file: {exc}",
                        payload={"filename": f.name, "error": str(exc)},
                    )
                )

    messages.sort(key=lambda m: (m.timestamp, m.message_id))
    return messages


def list_dead_letter_messages(
    recipient: str,
    mailbox_dir: Path | str | None = None,
) -> list[Message]:
    """List quarantined messages from dead-letter/ directory."""
    clean_recip = sanitize_recipient(recipient)
    root = resolve_mailbox_root(mailbox_dir)
    dead_letter_dir = root / clean_recip / DIR_DEAD_LETTER
    if not dead_letter_dir.exists():
        return []

    messages: list[Message] = []
    files = sorted([f for f in dead_letter_dir.iterdir() if f.is_file()], key=lambda p: p.name)
    for f in files:
        try:
            raw = f.read_text(encoding="utf-8")
            messages.append(Message.from_json(raw))
        except Exception as exc:
            messages.append(
                Message(
                    message_id=f.stem,
                    timestamp=datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%S.%fZ"
                    ),
                    sender="unknown",
                    recipient=clean_recip,
                    type="corrupted",
                    body=f"Corrupted file: {exc}",
                    payload={"filename": f.name, "error": str(exc)},
                )
            )

    messages.sort(key=lambda m: (m.timestamp, m.message_id))
    return messages


def read_message(
    *args: Any,
    recipient: str | None = None,
    message_id: str | None = None,
    ack: bool = True,
    mailbox_dir: Path | str | None = None,
    **kwargs: Any,
) -> Message:
    """Retrieve message by ID. If ack=True, moves from incoming/ to read/ idempotently."""
    if "to" in kwargs and recipient is None:
        recipient = kwargs.pop("to")
    if "id" in kwargs and message_id is None:
        message_id = kwargs.pop("id")
    if "msg_id" in kwargs and message_id is None:
        message_id = kwargs.pop("msg_id")
    if "root" in kwargs and mailbox_dir is None:
        mailbox_dir = kwargs.pop("root")

    pos_args = list(args)
    if pos_args:
        first = pos_args[0]
        if isinstance(first, Path) or (isinstance(first, str) and ("/" in first or "\\" in first)):
            if mailbox_dir is None:
                mailbox_dir = first
            pos_args = pos_args[1:]

    if pos_args and recipient is None:
        recipient = pos_args.pop(0)
    if pos_args and message_id is None:
        message_id = pos_args.pop(0)
    if pos_args:
        ack = bool(pos_args.pop(0))

    if recipient is None or not str(recipient).strip():
        raise InvalidMessageError("Recipient cannot be empty")
    clean_recip = sanitize_recipient(str(recipient))

    root = resolve_mailbox_root(mailbox_dir)
    _, incoming_dir, read_dir, dead_letter_dir = get_recipient_dirs(root, clean_recip)

    if message_id is None:
        unreads = list_messages(recipient=clean_recip, unread_only=True, mailbox_dir=root)
        if not unreads:
            raise MessageNotFoundError(f"No unread messages found for recipient '{clean_recip}'")
        message_id = unreads[0].message_id

    clean_id = validate_message_id(message_id)
    filename = f"{clean_id}.json"

    incoming_file = incoming_dir / filename
    read_file = read_dir / filename
    dead_letter_file = dead_letter_dir / filename

    # 1. Check incoming
    if incoming_file.exists():
        msg = _parse_or_quarantine_file(incoming_file, dead_letter_dir, clean_recip)
        if msg is not None:
            if ack:
                try:
                    os.replace(incoming_file, read_file)
                except FileNotFoundError:
                    if not read_file.exists():
                        raise MessageNotFoundError(f"Message '{clean_id}' disappeared during read")
            return msg
        if dead_letter_file.exists():
            raise CorruptedMessageError(
                f"Message '{clean_id}' was unparseable/corrupted and moved to dead-letter"
            )

    # 2. Check read (already acknowledged)
    if read_file.exists():
        try:
            raw = read_file.read_text(encoding="utf-8")
            return Message.from_json(raw)
        except FileNotFoundError:
            pass
        except Exception as exc:
            raise CorruptedMessageError(f"Message '{clean_id}' in read/ is corrupted: {exc}") from exc

    # 3. Check dead-letter
    if dead_letter_file.exists():
        raise CorruptedMessageError(f"Message '{clean_id}' is quarantined in dead-letter")

    raise MessageNotFoundError(f"Message '{clean_id}' not found for recipient '{clean_recip}'")


def ack_message(
    *args: Any,
    recipient: str | None = None,
    message_id: str | None = None,
    mailbox_dir: Path | str | None = None,
    **kwargs: Any,
) -> bool:
    """Atomically acknowledge a message by moving it to read/. Idempotent."""
    if "to" in kwargs and recipient is None:
        recipient = kwargs.pop("to")
    if "id" in kwargs and message_id is None:
        message_id = kwargs.pop("id")
    if "msg_id" in kwargs and message_id is None:
        message_id = kwargs.pop("msg_id")
    if "root" in kwargs and mailbox_dir is None:
        mailbox_dir = kwargs.pop("root")

    pos_args = list(args)
    if pos_args:
        first = pos_args[0]
        if isinstance(first, Path) or (isinstance(first, str) and ("/" in first or "\\" in first)):
            if mailbox_dir is None:
                mailbox_dir = first
            pos_args = pos_args[1:]

    if pos_args and recipient is None:
        recipient = pos_args.pop(0)
    if pos_args and message_id is None:
        message_id = pos_args.pop(0)

    if recipient is None or not str(recipient).strip():
        raise InvalidMessageError("Recipient cannot be empty")
    clean_recip = sanitize_recipient(str(recipient))

    if message_id is None or not str(message_id).strip():
        raise InvalidMessageError("message_id is required for ack_message")
    clean_id = validate_message_id(message_id)

    root = resolve_mailbox_root(mailbox_dir)
    _, incoming_dir, read_dir, dead_letter_dir = get_recipient_dirs(root, clean_recip)

    filename = f"{clean_id}.json"
    incoming_file = incoming_dir / filename
    read_file = read_dir / filename

    if incoming_file.exists():
        try:
            os.replace(incoming_file, read_file)
            return True
        except FileNotFoundError:
            if read_file.exists():
                return True
            raise MessageNotFoundError(f"Message '{clean_id}' not found for recipient '{clean_recip}'")

    if read_file.exists():
        return True

    if (dead_letter_dir / filename).exists():
        raise CorruptedMessageError(f"Message '{clean_id}' is quarantined in dead-letter")

    raise MessageNotFoundError(f"Message '{clean_id}' not found for recipient '{clean_recip}'")


def drain_inbox(
    *args: Any,
    recipient: str | None = None,
    mailbox_dir: Path | str | None = None,
    **kwargs: Any,
) -> list[Message]:
    """Retrieve and acknowledge all incoming messages chronologically."""
    if "to" in kwargs and recipient is None:
        recipient = kwargs.pop("to")
    if "root" in kwargs and mailbox_dir is None:
        mailbox_dir = kwargs.pop("root")

    pos_args = list(args)
    if pos_args:
        first = pos_args[0]
        if isinstance(first, Path) or (isinstance(first, str) and ("/" in first or "\\" in first)):
            if mailbox_dir is None:
                mailbox_dir = first
            pos_args = pos_args[1:]

    if pos_args and recipient is None:
        recipient = pos_args.pop(0)

    if recipient is None or not str(recipient).strip():
        raise InvalidMessageError("Recipient cannot be empty")
    clean_recip = sanitize_recipient(str(recipient))

    root = resolve_mailbox_root(mailbox_dir)
    recip_dir = root / clean_recip
    if not recip_dir.exists():
        return []

    incoming_dir = recip_dir / DIR_INCOMING
    read_dir = recip_dir / DIR_READ
    dead_letter_dir = recip_dir / DIR_DEAD_LETTER

    if not incoming_dir.exists():
        return []

    files = sorted([f for f in incoming_dir.iterdir() if f.is_file()], key=lambda p: p.name)
    drained: list[Message] = []

    for f in files:
        msg = _parse_or_quarantine_file(f, dead_letter_dir, clean_recip)
        if msg is not None:
            dest = read_dir / f.name
            try:
                os.replace(f, dest)
                drained.append(msg)
            except FileNotFoundError:
                # Concurrently moved by another process
                continue

    drained.sort(key=lambda m: (m.timestamp, m.message_id))
    return drained


# Survey/convenience alias
drain = drain_inbox


# ---------------------------------------------------------------------------
# Command-Line Interface (CLI)
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inbox.py",
        description="File-backed bidirectional mailbox system for chief-of-stuff.",
    )
    parser.add_argument(
        "--mailbox-dir",
        "--root",
        dest="global_mailbox_dir",
        help="Path to mailbox storage root directory",
    )

    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # send
    p_send = subparsers.add_parser("send", help="Send a message to a recipient's mailbox")
    p_send.add_argument("--to", "--recipient", dest="recipient", required=True, help="Recipient address/name")
    p_send.add_argument("--from", "--sender", dest="sender", required=True, help="Sender address/name")
    p_send.add_argument("--type", dest="type", default="generic", help="Message type (default: generic)")
    p_send.add_argument("--body", dest="body", default="", help="Message body text")
    p_send.add_argument("--payload", dest="payload", help="Message payload as JSON string")
    p_send.add_argument("--payload-file", dest="payload_file", help="Path to JSON payload file")
    p_send.add_argument("--task", dest="task", help="Associated task name")
    p_send.add_argument("--worktree", dest="worktree", help="Associated worktree path")
    p_send.add_argument("--header", dest="headers", action="append", help="Header key=value (repeatable)")
    p_send.add_argument("--format", dest="format", choices=["json", "text"], default="json", help="Output format")
    p_send.add_argument("--mailbox-dir", "--root", dest="mailbox_dir", help="Mailbox root directory")

    # list
    p_list = subparsers.add_parser("list", help="List messages in a mailbox")
    p_list.add_argument("--recipient", "--to", dest="recipient", required=True, help="Recipient address/name")
    filter_group = p_list.add_mutually_exclusive_group()
    filter_group.add_argument(
        "--unread",
        "--unread-only",
        dest="mode",
        action="store_const",
        const="unread",
        default="unread",
        help="List unread messages (default)",
    )
    filter_group.add_argument(
        "--all",
        dest="mode",
        action="store_const",
        const="all",
        help="List all messages (incoming and read)",
    )
    filter_group.add_argument(
        "--dead-letter",
        dest="mode",
        action="store_const",
        const="dead-letter",
        help="List dead-letter messages",
    )
    p_list.add_argument("--format", dest="format", choices=["json", "text"], default="json", help="Output format")
    p_list.add_argument("--json", dest="json_flag", action="store_true", help="Alias for --format json")
    p_list.add_argument("--mailbox-dir", "--root", dest="mailbox_dir", help="Mailbox root directory")

    # read
    p_read = subparsers.add_parser("read", help="Read a message from a mailbox")
    p_read.add_argument("message_id", help="Message ID to read")
    p_read.add_argument("--recipient", "--to", dest="recipient", required=True, help="Recipient address/name")
    ack_group = p_read.add_mutually_exclusive_group()
    ack_group.add_argument("--ack", dest="ack", action="store_true", default=True, help="Acknowledge message (default)")
    ack_group.add_argument("--no-ack", dest="ack", action="store_false", help="Do not acknowledge message")
    p_read.add_argument("--format", dest="format", choices=["json", "text"], default="json", help="Output format")
    p_read.add_argument("--json", dest="json_flag", action="store_true", help="Alias for --format json")
    p_read.add_argument("--mailbox-dir", "--root", dest="mailbox_dir", help="Mailbox root directory")

    # ack
    p_ack = subparsers.add_parser("ack", help="Acknowledge a message in a mailbox")
    p_ack.add_argument("message_id", help="Message ID to acknowledge")
    p_ack.add_argument("--recipient", "--to", dest="recipient", required=True, help="Recipient address/name")
    p_ack.add_argument("--mailbox-dir", "--root", dest="mailbox_dir", help="Mailbox root directory")

    # drain
    p_drain = subparsers.add_parser("drain", help="Drain and acknowledge all unread messages")
    p_drain.add_argument("--recipient", "--to", dest="recipient", required=True, help="Recipient address/name")
    p_drain.add_argument("--format", dest="format", choices=["json", "text"], default="json", help="Output format")
    p_drain.add_argument("--json", dest="json_flag", action="store_true", help="Alias for --format json")
    p_drain.add_argument("--mailbox-dir", "--root", dest="mailbox_dir", help="Mailbox root directory")

    # status
    p_status = subparsers.add_parser("status", help="Show message counts for a recipient")
    p_status.add_argument("--recipient", "--to", dest="recipient", required=True, help="Recipient address/name")
    p_status.add_argument("--format", dest="format", choices=["json", "text"], default="json", help="Output format")
    p_status.add_argument("--mailbox-dir", "--root", dest="mailbox_dir", help="Mailbox root directory")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if not args.subcommand:
        parser.print_help(sys.stderr)
        return 2

    mailbox_dir = getattr(args, "mailbox_dir", None) or args.global_mailbox_dir

    if args.subcommand == "send":
        headers_dict: dict[str, Any] = {}
        if args.headers:
            for h in args.headers:
                if "=" in h:
                    k, v = h.split("=", 1)
                    headers_dict[k.strip()] = v.strip()
                else:
                    headers_dict[h.strip()] = True

        payload = None
        if args.payload:
            try:
                payload = json.loads(args.payload)
            except json.JSONDecodeError as exc:
                sys.stderr.write(f"Error: Invalid JSON in --payload: {exc}\n")
                return 2
        elif getattr(args, "payload_file", None):
            pf = Path(args.payload_file)
            if not pf.exists():
                sys.stderr.write(f"Error: Payload file not found: {args.payload_file}\n")
                return 2
            try:
                payload = json.loads(pf.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                sys.stderr.write(f"Error: Invalid JSON in --payload-file: {exc}\n")
                return 2

        try:
            msg = send_message(
                recipient=args.recipient,
                sender=args.sender,
                body=args.body,
                msg_type=args.type,
                payload=payload,
                task=args.task,
                worktree=args.worktree,
                headers=headers_dict,
                mailbox_dir=mailbox_dir,
            )
        except InvalidMessageError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 2
        except InboxError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 1
        except Exception as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 1

        if args.format == "json":
            print(msg.to_json(indent=2))
        else:
            print(f"Sent message {msg.message_id} to {msg.recipient}")
        return 0

    if args.subcommand == "list":
        fmt = "json" if getattr(args, "json_flag", False) else args.format
        try:
            if args.mode == "dead-letter":
                msgs = list_dead_letter_messages(recipient=args.recipient, mailbox_dir=mailbox_dir)
            elif args.mode == "all":
                msgs = list_messages(recipient=args.recipient, unread_only=False, mailbox_dir=mailbox_dir)
            else:
                msgs = list_messages(recipient=args.recipient, unread_only=True, mailbox_dir=mailbox_dir)
        except InvalidMessageError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 2
        except InboxError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 1
        except Exception as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 1

        if fmt == "json":
            print(json.dumps([m.to_dict() for m in msgs], indent=2))
        else:
            if not msgs:
                print("No messages found.")
            else:
                for m in msgs:
                    print(f"[{m.timestamp}] {m.message_id} ({m.type}) from {m.sender}: {m.body}")
        return 0

    if args.subcommand == "read":
        fmt = "json" if getattr(args, "json_flag", False) else args.format
        try:
            msg = read_message(
                recipient=args.recipient,
                message_id=args.message_id,
                ack=args.ack,
                mailbox_dir=mailbox_dir,
            )
        except InvalidMessageError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 2
        except (MessageNotFoundError, CorruptedMessageError, InboxError) as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 1
        except Exception as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 1

        if fmt == "json":
            print(msg.to_json(indent=2))
        else:
            print(f"ID: {msg.message_id}")
            print(f"Timestamp: {msg.timestamp}")
            print(f"From: {msg.sender}")
            print(f"To: {msg.recipient}")
            print(f"Type: {msg.type}")
            if msg.task:
                print(f"Task: {msg.task}")
            if msg.worktree:
                print(f"Worktree: {msg.worktree}")
            print(f"Body: {msg.body}")
        return 0

    if args.subcommand == "ack":
        try:
            ack_message(
                recipient=args.recipient,
                message_id=args.message_id,
                mailbox_dir=mailbox_dir,
            )
        except InvalidMessageError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 2
        except (MessageNotFoundError, CorruptedMessageError, InboxError) as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 1
        except Exception as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 1

        print(f"Acknowledged message {args.message_id}")
        return 0

    if args.subcommand == "drain":
        fmt = "json" if getattr(args, "json_flag", False) else args.format
        try:
            msgs = drain_inbox(
                recipient=args.recipient,
                mailbox_dir=mailbox_dir,
            )
        except InvalidMessageError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 2
        except InboxError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 1
        except Exception as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 1

        if fmt == "json":
            print(json.dumps([m.to_dict() for m in msgs], indent=2))
        else:
            if not msgs:
                print("No unread messages.")
            else:
                for m in msgs:
                    print(f"[{m.timestamp}] {m.message_id} ({m.type}) from {m.sender}: {m.body}")
        return 0

    if args.subcommand == "status":
        fmt = "json" if getattr(args, "json_flag", False) else args.format
        try:
            clean_recip = sanitize_recipient(args.recipient)
            unread_msgs = list_messages(recipient=clean_recip, unread_only=True, mailbox_dir=mailbox_dir)
            all_msgs = list_messages(recipient=clean_recip, unread_only=False, mailbox_dir=mailbox_dir)
            dead_msgs = list_dead_letter_messages(recipient=clean_recip, mailbox_dir=mailbox_dir)
            status_info = {
                "recipient": clean_recip,
                "unread": len(unread_msgs),
                "read": max(0, len(all_msgs) - len(unread_msgs)),
                "dead_letter": len(dead_msgs),
                "total": len(all_msgs) + len(dead_msgs),
            }
        except InvalidMessageError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 2
        except InboxError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 1
        except Exception as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 1

        if fmt == "json":
            print(json.dumps(status_info, indent=2))
        else:
            print(f"Recipient: {status_info['recipient']}")
            print(f"Unread: {status_info['unread']}")
            print(f"Read: {status_info['read']}")
            print(f"Dead letter: {status_info['dead_letter']}")
            print(f"Total: {status_info['total']}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
