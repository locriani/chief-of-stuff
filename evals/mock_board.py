"""Mock board publisher MCP server for eval runs.

Stands in for the Artifact tool: one tool, `publish`, that takes the PATH of an html file (never html
text) and an optional URL to republish to. Without a URL it mints `board-1`, `board-2`, ...; a URL is
accepted only if this server minted it or it was seeded with `--known-url` (a URL a previous session
published). Each publish copies the file into `--store` and appends a JSONL record to `--log`,
the same log the calendar mock writes, with `"tool": "publish"`.

    python3 mock_board.py --log calls.jsonl --store store/ --root <agent cwd> --tz America/Chicago [--known-url URL]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_PROTOCOL = "2025-06-18"

PUBLISH = {
    "name": "publish",
    "description": "Publish an html file as the board. `path` is a file path (relative to the workspace root or absolute inside it), never html text. Pass `url` to republish an existing board; omit it only for the first publish.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path of the html file to publish"},
            "url": {"type": "string", "description": "URL of the board to republish to"},
        },
        "required": ["path"],
    },
}


class Board:
    def __init__(self, log: Path, store: Path, root: Path, tz: ZoneInfo, known: list[str]) -> None:
        self.log, self.store, self.root, self.tz = log, store, root.resolve(), tz
        self.known = set(known)
        self.count = 0

    def publish(self, args: dict[str, Any]) -> dict[str, Any]:
        raw = str(args.get("path", ""))
        if "<" in raw or "\n" in raw or not raw.strip():
            return _error("path must be a file path, not html text")
        path = Path(raw)
        path = path if path.is_absolute() else self.root / path
        try:
            resolved = path.resolve()
            resolved.relative_to(self.root)
        except ValueError:
            return _error(f"path is outside the workspace root: {raw}")
        if not resolved.is_file():
            return _error(f"file not found: {raw}")
        url = args.get("url")
        if url and url not in self.known:
            return _error(f"unknown url: {url}")
        self.count += 1
        if not url:
            url = f"board-{self.count}"
            self.known.add(url)
        data = resolved.read_bytes()
        self.store.mkdir(parents=True, exist_ok=True)
        stored = self.store / f"publish-{self.count}.html"
        shutil.copyfile(resolved, stored)
        with self.log.open("a") as f:
            f.write(json.dumps({
                "tool": "publish",
                "arguments": args,
                "url": url,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "stored": str(stored),
                "at": datetime.now(self.tz).isoformat(),
            }) + "\n")
        return {"content": [{"type": "text", "text": json.dumps({"url": url, "bytes": len(data)})}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def handle(msg: dict[str, Any], board: Board) -> dict[str, Any] | None:
    method = msg.get("method")
    if "id" not in msg:
        return None
    reply: dict[str, Any] = {"jsonrpc": "2.0", "id": msg["id"]}
    params = msg.get("params") or {}
    if method == "initialize":
        reply["result"] = {
            "protocolVersion": params.get("protocolVersion", DEFAULT_PROTOCOL),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mock-board", "version": "0.1.0"},
        }
    elif method == "ping":
        reply["result"] = {}
    elif method == "tools/list":
        reply["result"] = {"tools": [PUBLISH]}
    elif method == "tools/call" and params.get("name") == "publish":
        reply["result"] = board.publish(params.get("arguments") or {})
    elif method == "tools/call":
        reply["error"] = {"code": -32602, "message": f"unknown tool {params.get('name')!r}"}
    else:
        reply["error"] = {"code": -32601, "message": f"method not found: {method}"}
    return reply


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--store", type=Path, required=True)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--tz", default="UTC")
    ap.add_argument("--known-url", action="append", default=[])
    args = ap.parse_args(argv)
    board = Board(args.log, args.store, args.root, ZoneInfo(args.tz), args.known_url)
    for line in sys.stdin:
        if not line.strip():
            continue
        reply = handle(json.loads(line), board)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
