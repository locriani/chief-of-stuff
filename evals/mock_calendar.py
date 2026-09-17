"""Mock calendar MCP server for eval runs.

Speaks newline-delimited JSON-RPC over stdio and exposes one tool, `list_events`, backed by a
rendered events file shaped like Google Calendar (`summary`, `start`/`end` with `dateTime` or
`date`, `transparency`). Every tool call is appended to a JSONL log for the `mock_calls` grader.

    python3 mock_calendar.py --events events.json --log calls.jsonl --tz America/Chicago
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_PROTOCOL = "2025-06-18"

LIST_EVENTS = {
    "name": "list_events",
    "description": "List events on one calendar, optionally limited to a time window. Returns Google Calendar-shaped event objects.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "calendarId": {"type": "string", "description": "Calendar id, e.g. cal-work"},
            "timeMin": {"type": "string", "description": "RFC3339 lower bound (exclusive of events ending at or before it)"},
            "timeMax": {"type": "string", "description": "RFC3339 upper bound (exclusive of events starting at or after it)"},
        },
        "required": ["calendarId"],
    },
}


def parse_instant(value: str, tz: ZoneInfo) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def event_bounds(event: dict[str, Any], tz: ZoneInfo) -> tuple[datetime, datetime]:
    def one(side: dict[str, Any]) -> datetime:
        if "dateTime" in side:
            return parse_instant(side["dateTime"], tz)
        return datetime.combine(date.fromisoformat(side["date"]), time(0), tz)

    return one(event["start"]), one(event["end"])


def list_events(events: dict[str, list[dict[str, Any]]], args: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
    cal = args.get("calendarId")
    if cal not in events:
        return {"content": [{"type": "text", "text": f"calendar not found: {cal}"}], "isError": True}
    lo = parse_instant(args["timeMin"], tz) if args.get("timeMin") else None
    hi = parse_instant(args["timeMax"], tz) if args.get("timeMax") else None
    items = []
    for event in events[cal]:
        start, end = event_bounds(event, tz)
        if (lo is None or end > lo) and (hi is None or start < hi):
            items.append(event)
    return {"content": [{"type": "text", "text": json.dumps({"calendarId": cal, "items": items})}]}


def handle(msg: dict[str, Any], events: dict[str, Any], tz: ZoneInfo, log: Path) -> dict[str, Any] | None:
    method = msg.get("method")
    if "id" not in msg:
        return None
    reply: dict[str, Any] = {"jsonrpc": "2.0", "id": msg["id"]}
    params = msg.get("params") or {}
    if method == "initialize":
        reply["result"] = {
            "protocolVersion": params.get("protocolVersion", DEFAULT_PROTOCOL),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mock-calendar", "version": "0.1.0"},
        }
    elif method == "ping":
        reply["result"] = {}
    elif method == "tools/list":
        reply["result"] = {"tools": [LIST_EVENTS]}
    elif method == "tools/call" and params.get("name") == "list_events":
        arguments = params.get("arguments") or {}
        with log.open("a") as f:
            f.write(json.dumps({"tool": "list_events", "arguments": arguments, "at": datetime.now(tz).isoformat()}) + "\n")
        reply["result"] = list_events(events, arguments, tz)
    elif method == "tools/call":
        reply["error"] = {"code": -32602, "message": f"unknown tool {params.get('name')!r}"}
    else:
        reply["error"] = {"code": -32601, "message": f"method not found: {method}"}
    return reply


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--events", type=Path, required=True)
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--tz", default="UTC")
    args = ap.parse_args(argv)
    events = json.loads(args.events.read_text())
    tz = ZoneInfo(args.tz)
    for line in sys.stdin:
        if not line.strip():
            continue
        reply = handle(json.loads(line), events, tz, args.log)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
