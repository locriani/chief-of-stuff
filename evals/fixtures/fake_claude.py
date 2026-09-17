#!/usr/bin/env python3
"""Stand-in for `claude -p --input-format stream-json` in runner unit tests.

Emits init, one assistant text, and a result per user line on stdin. Turn 2 writes `turn2.txt` in
cwd so tests can check per-turn fixture snapshots. A user message containing SLEEP hangs; one containing
BACKGROUND is followed by an unprompted notification turn; INLINE carries the notification inside its own turn; LINGER keeps the process alive after stdin closes.
"""

import json
import sys
import time
from pathlib import Path



def turn(text):
    return (
        {"type": "system", "subtype": "init", "tools": ["Bash"], "agents": ["claude"], "mcp_servers": []},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
        {"type": "result", "subtype": "success", "result": text, "num_turns": 1, "permission_denials": []},
    )


n = 0
linger = False
for line in sys.stdin:
    if not line.strip():
        continue
    n += 1
    text = json.loads(line)["message"]["content"]
    linger = linger or "LINGER" in text
    if "SLEEP" in text:
        time.sleep(30)
    if n == 2:
        Path("turn2.txt").write_text("written in turn 2\n")
    if "INLINE" in text:
        # The background task finishes before the assistant stops: the notification lands inside this turn.
        init, reply, result = turn(f"reply {n}: {text}")
        final = f"reply {n}: {text} — handled inline: DONE"
        for ev in (init, reply, {"type": "system", "subtype": "task_notification"}, {"type": "assistant", "message": {"content": [{"type": "text", "text": final}]}}, dict(result, result=final)):
            print(json.dumps(ev), flush=True)
        continue
    for ev in turn(f"reply {n}: {text}"):
        print(json.dumps(ev), flush=True)
    if "BACKGROUND" in text:
        # A background task finishing starts a turn with no user message, as `claude -p` does.
        time.sleep(0.5)
        print(json.dumps({"type": "system", "subtype": "task_notification"}), flush=True)
        for ev in turn("notified: DONE"):
            print(json.dumps(ev), flush=True)
if linger:
    # A background task still running after stdin closes keeps the process alive.
    time.sleep(60)
