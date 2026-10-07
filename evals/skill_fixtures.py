"""Generic plugin roots, written only inside a caller's temporary directory (#484)."""

import json
from pathlib import Path

AGENT = """---
name: chief-of-stuff
description: A generic fixture coordinator.
---
## Role

Before a generic task, Read ${CLAUDE_PLUGIN_ROOT}/skills/alpha/SKILL.md.
Run python3 ${CLAUDE_PLUGIN_ROOT}/scripts/tool.py.

## Check

Check the generic task. CLAUDE_CHECK_ONLY

The standing waiting-on list stays generic.

## Sessions

CLAUDE_SESSIONS_ONLY

## Relay

RELAY_KEPT
"""


def write_plugin(root: Path, agent: str = AGENT) -> Path:
    files = {
        "agents/chief-of-stuff.md": agent,
        "scripts/tool.py": "# generic fixture tool\n",
        "assets/example.txt": "generic fixture asset\n",
        "start_coordinator.py": "# generic fixture entry point\n",
        "chief_of_stuff.py": "# generic fixture entry point\n",
        ".claude-plugin/plugin.json": json.dumps({"version": "0.0.0"}),
    }
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def write_skill(root: Path, name: str, body: str, frontmatter: str = "") -> Path:
    path = root / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(("---\n" + frontmatter.rstrip() + "\n---\n\n" if frontmatter else "") + body)
    return path
