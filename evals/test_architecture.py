"""What reads and writes the workspace never loads what draws it.

The page modules render HTML. Everything else — the tracker and coordinator models, the hook that runs before
every tool call, the launchers, the audit — has to import without them. When the tracker parser lived in the
board renderer, `chief-of-stuff models --class` failed on a syntax error in the decision page, and the
PreToolUse hook loaded the board, the page server and the forge cache to read one `Board:` line.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

PAGES = frozenset({"render_board", "decision_page", "issue_page", "source_page", "workers_page", "pages",
                   "flow_chart", "gantt", "panels", "columns", "fragment"})

# Every script module that draws nothing. A new one belongs here unless it renders a page.
DRAWS_NOTHING = (
    "md", "clock", "workspace", "tracker", "settings", "backlog_ref", "backlog", "ownership", "forge_review",
    "board_guard", "board_sources", "tracker_write", "make_worktree", "dispatch_prompt", "kanban", "audit_tasks",
    "one_shot", "spawn_session", "session_exec", "shell_setup", "process_status", "merge_approved",
    "review_threads", "notify", "init_workspace", "install_model_guidance", "migrate_backlog", "inbox",
    "probe_health", "runtimes",
)

PROBE = "import sys; sys.path.insert(0, sys.argv[1]); import {name}; print(' '.join(sorted(set(sys.modules) & set(sys.argv[2:]))))"

# The forge client's HTTP stack. Reading a `Backlog:` line needs none of it, and the hook runs before every tool call.
HTTP = ("urllib.request", "http.client", "ssl")


class DependencyRuleTest(unittest.TestCase):
    def test_every_script_is_classified(self) -> None:
        scripts = {p.stem for p in SCRIPTS.glob("*.py")}
        self.assertEqual(sorted(scripts - PAGES - set(DRAWS_NOTHING)), [])

    def test_a_module_that_draws_nothing_imports_no_page(self) -> None:
        for name in DRAWS_NOTHING:
            with self.subTest(module=name):
                proc = subprocess.run([sys.executable, "-c", PROBE.format(name=name), str(SCRIPTS), *sorted(PAGES)],
                                      capture_output=True, text=True, timeout=60)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.split(), [], f"{name} loads page modules")

    def test_the_hook_loads_no_http_client(self) -> None:
        proc = subprocess.run([sys.executable, "-c", PROBE.format(name="board_guard"), str(SCRIPTS), *HTTP],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split(), [], "board_guard loads the forge client")


if __name__ == "__main__":
    unittest.main()
