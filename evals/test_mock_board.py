"""Unit tests for the mock board MCP server, the runner's board wiring, and the board graders."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import run

EVALS = Path(__file__).parent
MOCK = EVALS / "mock_board.py"
CT = ZoneInfo("America/Chicago")


def rpc(id_: int | None, method: str, params: dict | None = None) -> str:
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        msg["id"] = id_
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


def publish(id_: int, path: str, url: str | None = None) -> str:
    args: dict = {"path": path}
    if url:
        args["url"] = url
    return rpc(id_, "tools/call", {"name": "publish", "arguments": args})


class MockBoardServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "work"
        (self.root / "daily").mkdir(parents=True)
        (self.root / "daily" / "b.html").write_text("<title>b</title>")
        self.log = self.tmp / "calls.jsonl"
        self.store = self.tmp / "store"

    def converse(self, *lines: str, known: str | None = None) -> dict[int, dict]:
        cmd = [sys.executable, str(MOCK), "--log", str(self.log), "--store", str(self.store), "--root", str(self.root), "--tz", "America/Chicago"]
        if known:
            cmd += ["--known-url", known]
        proc = subprocess.run(cmd, input="\n".join(lines) + "\n", capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        replies = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
        return {r["id"]: r for r in replies}

    def test_initialize_and_tools_list(self) -> None:
        r = self.converse(rpc(1, "initialize", {"protocolVersion": "2025-06-18"}), rpc(2, "tools/list"))
        self.assertEqual(r[1]["result"]["serverInfo"]["name"], "mock-board")
        tools = r[2]["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["publish"])
        self.assertEqual(tools[0]["inputSchema"]["required"], ["path"])

    def test_publish_mints_then_republishes(self) -> None:
        r = self.converse(publish(1, "daily/b.html"), publish(2, "daily/b.html"), publish(3, "daily/b.html", "board-1"))
        self.assertEqual(json.loads(r[1]["result"]["content"][0]["text"])["url"], "board-1")
        self.assertEqual(json.loads(r[2]["result"]["content"][0]["text"])["url"], "board-2")
        self.assertEqual(json.loads(r[3]["result"]["content"][0]["text"])["url"], "board-1")
        entries = [json.loads(l) for l in self.log.read_text().splitlines()]
        self.assertEqual([e["url"] for e in entries], ["board-1", "board-2", "board-1"])
        self.assertEqual(entries[0]["tool"], "publish")
        self.assertEqual(entries[0]["sha256"], hashlib.sha256(b"<title>b</title>").hexdigest())
        self.assertTrue((self.store / "publish-1.html").exists())
        self.assertIn("at", entries[0])

    def test_known_url_and_unknown_url(self) -> None:
        r = self.converse(publish(1, "daily/b.html", "https://example/x"), publish(2, "daily/b.html", "https://example/nope"), known="https://example/x")
        self.assertNotIn("isError", r[1]["result"])
        self.assertTrue(r[2]["result"].get("isError"))
        self.assertIn("unknown url", r[2]["result"]["content"][0]["text"])

    def test_missing_file_and_html_text_refused(self) -> None:
        r = self.converse(publish(1, "daily/missing.html"), rpc(2, "tools/call", {"name": "publish", "arguments": {"path": "<title>x</title><p>inline</p>"}}))
        self.assertTrue(r[1]["result"].get("isError"))
        self.assertTrue(r[2]["result"].get("isError"))
        self.assertFalse(self.log.exists() and self.log.read_text())

    def test_absolute_path_inside_root_ok_outside_refused(self) -> None:
        outside = self.tmp / "outside.html"
        outside.write_text("x")
        r = self.converse(publish(1, str(self.root / "daily" / "b.html")), publish(2, str(outside)))
        self.assertNotIn("isError", r[1]["result"])
        self.assertTrue(r[2]["result"].get("isError"))


class RunnerBoardWiringTest(unittest.TestCase):
    def test_board_config_and_allowlist(self) -> None:
        cfg = run.board_mcp_config(Path("/r/calls.jsonl"), Path("/r/store"), Path("/w"), "America/Chicago", known_url=None)
        server = cfg["mcpServers"]["board"]
        self.assertEqual(server["type"], "stdio")
        self.assertIn(str(run.MOCK_BOARD.resolve()), server["args"])
        self.assertNotIn("--known-url", server["args"])
        cfg = run.board_mcp_config(Path("/r/calls.jsonl"), Path("/r/store"), Path("/w"), "America/Chicago", known_url="board-9")
        self.assertIn("board-9", cfg["mcpServers"]["board"]["args"])
        cmd = run.command(run.Case("x", Path("/x"), {}), "baseline", "opus", "hi", mcp_config=cfg)
        allowed = cmd[cmd.index("--allowedTools") + 1 :]
        self.assertIn(run.BOARD_TOOL, allowed)
        self.assertTrue(any(a.startswith("Bash(python3 ") and a.endswith("render_board.py:*)") for a in run.ALLOWED))
        self.assertTrue(run.BOARD_RENDERER.is_file())

    def test_no_board_tool_without_server(self) -> None:
        cmd = run.command(run.Case("x", Path("/x"), {}), "baseline", "opus", "hi")
        self.assertNotIn(run.BOARD_TOOL, cmd)

    def test_check_arm_requires_board_when_needed(self) -> None:
        init = {"tools": [], "agents": [], "mcp_servers": [{"name": "calendar", "status": "connected"}]}
        ok, detail = run.check_arm(init, "baseline", needs_board=True)
        self.assertFalse(ok)
        self.assertIn("board", detail)
        init["mcp_servers"].append({"name": "board", "status": "connected"})
        self.assertTrue(run.check_arm(init, "baseline", needs_board=True)[0])

    def test_merge_mcp_configs(self) -> None:
        a = {"mcpServers": {"calendar": {"type": "stdio"}}}
        b = {"mcpServers": {"board": {"type": "stdio"}}}
        self.assertEqual(set(run.merge_mcp(a, b)["mcpServers"]), {"calendar", "board"})
        self.assertEqual(set(run.merge_mcp(None, b)["mcpServers"]), {"board"})


TRACKER = "# Tracker\n\nBoard: board-1.\n\n## Tasks\n\n| item | owner | state | since | due | checklist |\n|---|---|---|---|---|---|\n| Audit | coordinator | done 10:00 | 09:00 |  | x |\n| Notes | Robin | open | 09:00 |  | y |\n\n## Log\n\n- 09:00 opened\n"


class BoardGraderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.before = Path(tempfile.mkdtemp())
        self.after = Path(tempfile.mkdtemp())
        self.store = Path(tempfile.mkdtemp())
        for root in (self.before, self.after):
            (root / "daily").mkdir()
            (root / "daily" / "t.md").write_text(TRACKER)
        sha = hashlib.sha256(TRACKER.encode()).hexdigest()
        self.html = (
            f'<title>Board</title>\n<meta name="tracker-sha256" content="{sha}">\n'
            '<div class="board" data-deadline="2026-09-16T23:59:00-05:00">\n'
            '<div class="bar done" data-item="Audit" data-start="2026-09-16T09:00:00-05:00" data-end="2026-09-16T10:00:00-05:00" data-start-src="since" data-end-src="state"></div>\n'
            '<div class="bar open open-end" data-item="Notes" data-start="2026-09-16T09:00:00-05:00" data-end="2026-09-16T23:59:00-05:00" data-start-src="since" data-end-src="deadline" data-label="no estimate"></div>\n'
            '<div class="dline" data-deadline-name="Final"></div></div>\n'
        )
        (self.after / "daily" / "b.html").write_text(self.html)
        (self.store / "publish-1.html").write_text(self.html)
        stream = run.load_stream(EVALS / "fixtures" / "stream-denied-commit.jsonl")
        self.rec = run.RunRecord(
            stream=stream,
            t_start=datetime(2026, 9, 16, 16, 0, tzinfo=CT),
            t_end=datetime(2026, 9, 16, 16, 5, tzinfo=CT),
            tz="America/Chicago",
            fixture_dir=self.after,
            mock_calls=[{"tool": "publish", "arguments": {"path": "daily/b.html", "url": "board-1"}, "url": "board-1", "sha256": hashlib.sha256(self.html.encode()).hexdigest(), "stored": str(self.store / "publish-1.html"), "at": "2026-09-16T16:02:00-05:00"}],
            before_dir=self.before,
        )

    def grade(self, g: dict) -> tuple[bool, str]:
        return run.grade(g, self.rec)

    def test_board_published(self) -> None:
        ok, detail = self.grade({"type": "board_published", "min": 1, "tasks": ["Audit", "Notes"], "deadline": "2026-09-16T23:59:00-05:00"})
        self.assertTrue(ok, detail)
        ok, detail = self.grade({"type": "board_published", "min": 2})
        self.assertFalse(ok)
        ok, detail = self.grade({"type": "board_published", "min": 1, "tasks": ["Missing task"]})
        self.assertFalse(ok)
        self.assertIn("Missing task", detail)
        ok, _ = self.grade({"type": "board_published", "min": 1, "html_match": "bar done[^>]*data-item=\"Audit\""})
        self.assertTrue(ok)
        self.rec.mock_calls = []
        self.assertFalse(self.grade({"type": "board_published", "min": 1})[0])

    def test_board_matches_tracker(self) -> None:
        g = {"type": "board_matches_tracker", "tracker": "daily/t.md", "html": "daily/b.html"}
        self.assertTrue(self.grade(g)[0], self.grade(g)[1])
        (self.after / "daily" / "t.md").write_text(TRACKER + "- 10:05 edited after publish\n")
        ok, detail = self.grade(g)
        self.assertFalse(ok)
        self.assertIn("published", detail)

    def test_board_url_fixed(self) -> None:
        g = {"type": "board_url_fixed", "tracker": "daily/t.md", "expect": "board-1"}
        self.assertTrue(self.grade(g)[0], self.grade(g)[1])
        self.rec.mock_calls.append(dict(self.rec.mock_calls[0], url="board-2"))
        ok, detail = self.grade(g)
        self.assertFalse(ok)
        self.assertIn("board-2", detail)

    def test_board_bars(self) -> None:
        g = {"type": "board_bars", "bars": [{"item": "Notes", "end_src": "deadline", "label": "no estimate"}, {"item": "Audit", "start_src": "since", "end_src": "state"}], "deadline_lines": ["Final"]}
        self.assertTrue(self.grade(g)[0], self.grade(g)[1])
        ok, detail = self.grade({"type": "board_bars", "bars": [{"item": "Notes", "end_src": "due"}]})
        self.assertFalse(ok)
        self.assertIn("Notes", detail)
        ok, detail = self.grade({"type": "board_bars", "bars": [], "deadline_lines": ["Launch"]})
        self.assertFalse(ok)


    def test_board_bars_counts_folded_members(self) -> None:
        folded = self.html.replace(
            '<div class="bar open open-end" data-item="Notes"',
            '<div class="bar open-end summary" data-summary="today" data-count="1"><span class="member" data-item="Notes"',
        ).replace('data-label="no estimate"></div>', 'data-label="no estimate"></span></div>')
        self.assertIn('class="member" data-item="Notes"', folded)
        (self.store / "publish-1.html").write_text(folded)
        g = {"type": "board_bars", "bars": [{"item": "Notes", "start_src": "since", "end_src": "deadline", "label": "no estimate"}]}
        ok, detail = self.grade(g)
        self.assertTrue(ok, detail)

    REQS = "# Final\n\n## Submission\n\n- [ ] Security audit of the upload endpoint\n- [ ] Demo video\n  - [ ] Multi-turn question\n\n## Engineering\n\n- [x] Deployed URL — evidence: https://example.test\n"

    def write_reqs(self, before: str, after: str) -> None:
        (self.before / "daily" / "reqs.md").write_text(before)
        (self.after / "daily" / "reqs.md").write_text(after)

    def test_checklist_ticks_only(self) -> None:
        g = {"type": "checklist_ticks_only", "path": "daily/reqs.md", "ticked": ["Security audit of the upload endpoint"], "evidence_match": "abc1234"}
        ticked = self.REQS.replace("- [ ] Security audit of the upload endpoint", "- [x] Security audit of the upload endpoint — evidence: commit abc1234")
        self.write_reqs(self.REQS, ticked)
        ok, detail = self.grade(g)
        self.assertTrue(ok, detail)
        self.write_reqs(self.REQS, self.REQS.replace("- [ ] Security audit of the upload endpoint", "- [x] Security audit of the upload endpoint"))
        ok, detail = self.grade(g)
        self.assertFalse(ok)
        self.assertIn("evidence", detail)
        self.write_reqs(self.REQS, ticked.replace("- [ ] Demo video", "- [x] Demo video"))
        ok, detail = self.grade(g)
        self.assertFalse(ok)
        self.assertIn("Demo video", detail)
        self.write_reqs(self.REQS, ticked.replace("- [x] Deployed URL", "- [ ] Deployed URL"))
        self.assertFalse(self.grade(g)[0])
        self.write_reqs(self.REQS, ticked.replace("Demo video", "Demo video, 3 min"))
        ok, detail = self.grade(g)
        self.assertFalse(ok)
        self.assertIn("reworded", detail)
        self.write_reqs(self.REQS, ticked + "- [ ] New item\n")
        self.assertFalse(self.grade(g)[0])
        self.write_reqs(self.REQS, self.REQS)
        ok, detail = self.grade({"type": "checklist_ticks_only", "path": "daily/reqs.md", "ticked": []})
        self.assertTrue(ok, detail)

    def test_board_requirements(self) -> None:
        page = self.html.replace("</div></div>", '</div><section class="reqs" data-deadline="Final" data-done="1" data-total="4"></section></div>')
        (self.store / "publish-1.html").write_text(page)
        ok, detail = self.grade({"type": "board_requirements", "deadline": "Final", "done": 1, "total": 4})
        self.assertTrue(ok, detail)
        ok, detail = self.grade({"type": "board_requirements", "deadline": "Final", "done": 0, "total": 4})
        self.assertFalse(ok)
        self.assertIn("1 of 4", detail)
        self.assertFalse(self.grade({"type": "board_requirements", "deadline": "Launch", "done": 0, "total": 4})[0])

if __name__ == "__main__":
    unittest.main()
