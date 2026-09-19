"""Mock peer-sessions MCP server for eval runs.

Stands in for the real `ListAgents` and `SendMessage` tools, which never enter a run. Two tools:
`list_sessions` prints rows shaped exactly like the live listing (`name [ref]  ·  interactive  ·
idle|busy|waiting  ·  started Nh ago`); `send {to, text}` resolves `to` as a name or `name [ref]`
(a bare ref is refused, like the live tool). The sessions file is re-read on every call so a case can
rename a session between turns. Every call is appended to the shared JSONL log.

    python3 mock_peers.py --sessions sessions.json --log calls.jsonl --tz America/Chicago
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_PROTOCOL = "2025-06-18"
NAME_REF = re.compile(r"^(.*?)\s*\[([0-9a-f]{6})\]$")

LIST_SESSIONS = {
    "name": "list_sessions",
    "description": "List the user's other Claude sessions on this machine: one row per session, `name [ref] · interactive · idle|busy|waiting · started Nh ago`. The ref is stable; names change.",
    "inputSchema": {"type": "object", "properties": {}},
}
SEND = {
    "name": "send",
    "description": "Send a message to another session. `to` is its current name, or `name [ref]`. The first line of `text` is the preview the session's user sees.",
    "inputSchema": {
        "type": "object",
        "properties": {"to": {"type": "string"}, "text": {"type": "string"}},
        "required": ["to", "text"],
    },
}


def _age(s: dict[str, Any]) -> str:
    """`started_minutes_ago` when a case sets it — a session that came up moments ago is a premise, not a detail — else hours."""
    if "started_minutes_ago" in s:
        return f"{s['started_minutes_ago']}m"
    return f"{s.get('started_hours_ago', 1)}h"


def _rows(sessions: list[dict[str, Any]]) -> str:
    return "\n".join(f"{s['name']} [{s['ref']}]  ·  interactive  ·  {s.get('state', 'idle')}  ·  started {_age(s)} ago" for s in sessions)


def resolve(to: str, sessions: list[dict[str, Any]]) -> dict[str, Any] | None:
    m = NAME_REF.match(to.strip())
    if m:
        return next((s for s in sessions if s["ref"] == m.group(2)), None)
    return next((s for s in sessions if s["name"] == to.strip()), None)


class Peers:
    def __init__(self, sessions_file: Path, log: Path, tz: ZoneInfo) -> None:
        self.sessions_file, self.log, self.tz = sessions_file, log, tz

    def sessions(self) -> list[dict[str, Any]]:
        return json.loads(self.sessions_file.read_text())

    def record(self, entry: dict[str, Any]) -> None:
        with self.log.open("a") as f:
            f.write(json.dumps(dict(entry, at=datetime.now(self.tz).isoformat())) + "\n")

    def list_sessions(self) -> dict[str, Any]:
        self.record({"tool": "list_sessions"})
        return {"content": [{"type": "text", "text": _rows(self.sessions())}]}

    def send(self, args: dict[str, Any]) -> dict[str, Any]:
        to, text = str(args.get("to", "")), str(args.get("text", ""))
        target = resolve(to, self.sessions())
        self.record({"tool": "send", "to": to, "to_ref": target["ref"] if target else None, "text": text, "ok": target is not None})
        if target is None:
            return {"content": [{"type": "text", "text": f"no agent reachable: {to}. List sessions again; names change."}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps({"ok": True, "to": f"{target['name']} [{target['ref']}]"})}]}


def handle(msg: dict[str, Any], peers: Peers) -> dict[str, Any] | None:
    method = msg.get("method")
    if "id" not in msg:
        return None
    reply: dict[str, Any] = {"jsonrpc": "2.0", "id": msg["id"]}
    params = msg.get("params") or {}
    if method == "initialize":
        reply["result"] = {
            "protocolVersion": params.get("protocolVersion", DEFAULT_PROTOCOL),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mock-peers", "version": "0.1.0"},
        }
    elif method == "ping":
        reply["result"] = {}
    elif method == "tools/list":
        reply["result"] = {"tools": [LIST_SESSIONS, SEND]}
    elif method == "tools/call" and params.get("name") == "list_sessions":
        reply["result"] = peers.list_sessions()
    elif method == "tools/call" and params.get("name") == "send":
        reply["result"] = peers.send(params.get("arguments") or {})
    elif method == "tools/call":
        reply["error"] = {"code": -32602, "message": f"unknown tool {params.get('name')!r}"}
    else:
        reply["error"] = {"code": -32601, "message": f"method not found: {method}"}
    return reply


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sessions", type=Path, required=True)
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--tz", default="UTC")
    args = ap.parse_args(argv)
    peers = Peers(args.sessions, args.log, ZoneInfo(args.tz))
    for line in sys.stdin:
        if not line.strip():
            continue
        reply = handle(json.loads(line), peers)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
