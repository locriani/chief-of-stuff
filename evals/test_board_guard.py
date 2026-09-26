"""The Claude hook prevents board navigation without blocking board maintenance."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import board_guard  # noqa: E402


class BoardGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n"
            "- Tracker: `daily/<date>-tracker.md`\n- Timezone: America/Chicago\n"
            "- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages`\n")
        self.env = mock.patch.dict(os.environ, {"CHIEF_OF_STUFF_WORKSPACE": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def event(self, tool, **inputs):
        return {"cwd": str(self.root), "tool_name": tool, "tool_input": inputs}

    def test_shell_browser_launchers_are_blocked_for_the_board(self):
        commands = (
            'open http://127.0.0.1:8765/',
            '/usr/bin/open -a "Google Chrome" http://localhost:8765/all',
            'xdg-open "http://127.0.0.1:8765/2026-09-25-board.html"',
            'python3 -c "import webbrowser; webbrowser.open(\'http://localhost:8765/\')"',
            'osascript -e \'open location "http://127.0.0.1:8765/"\'',
            'node -e "page.goto(\'http://localhost:8765/\')"',
            'open pages/2026-09-25-board.html',
            f'open "file://{self.root}/pages/2026-09-25-board.html"',
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(board_guard.denied(self.event("Bash", command=command)))

    def test_browser_mcp_navigation_is_blocked(self):
        for name in ("mcp__claude_in_chrome__navigate", "mcp__playwright__browser_navigate",
                     "mcp__browser__open_tab", "mcp__artifacts__open"):
            with self.subTest(tool=name):
                self.assertTrue(board_guard.denied(self.event(name, url="http://localhost:8765/")))
        self.assertTrue(board_guard.denied(self.event("mcp__cua__run_code",
            code="await tab.goto('http://localhost:8765/')")))
        self.assertTrue(board_guard.denied(self.event("mcp__cua_repl__js",
            code="await cua.createBrowserTab('chrome', 'http://localhost:8765/')")))

    def test_checks_rendering_serving_and_unrelated_navigation_remain_available(self):
        events = (
            self.event("Bash", command="curl -I http://127.0.0.1:8765/"),
            self.event("Bash", command="chief-of-stuff pages --ensure"),
            self.event("Bash", command="chief-of-stuff board --date 2026-09-25"),
            self.event("Bash", command="open http://localhost:3000/"),
            self.event("WebFetch", url="http://127.0.0.1:8765/"),
            self.event("Write", file_path="CLAUDE.md", content="open http://127.0.0.1:8765/"),
            self.event("mcp__browser__navigate", url="http://localhost:3000/"),
        )
        for event in events:
            with self.subTest(event=event):
                self.assertFalse(board_guard.denied(event))

    def test_worktree_hook_uses_workspace_configuration(self):
        with tempfile.TemporaryDirectory() as tree:
            event = self.event("Bash", command="open http://127.0.0.1:8765/")
            event["cwd"] = tree
            self.assertTrue(board_guard.denied(event))

    def test_native_plugin_discovers_workspace_in_ancestors(self):
        (self.root / "nested").mkdir()
        with mock.patch.dict(os.environ):
            os.environ.pop("CHIEF_OF_STUFF_WORKSPACE", None)
            event = self.event("Bash", command="open http://127.0.0.1:8765/")
            event["cwd"] = str(self.root / "nested")
            self.assertTrue(board_guard.denied(event))

    def test_hook_protocol_denies_before_execution_and_says_why(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/board_guard.py")],
                                input=json.dumps(self.event("Bash", command="open http://localhost:8765/")),
                                text=True, capture_output=True, check=True)
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("user decides", decision["permissionDecisionReason"])
        allowed = subprocess.run([sys.executable, str(ROOT / "scripts/board_guard.py")],
                                 input=json.dumps(self.event("Bash", command="curl http://localhost:8765/")),
                                 text=True, capture_output=True, check=True)
        self.assertEqual(allowed.stdout, "")


if __name__ == "__main__":
    unittest.main()
