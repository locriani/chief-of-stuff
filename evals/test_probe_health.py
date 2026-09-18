"""Unit tests for scripts/probe_health.py: health facts come from a probe, never from the agent's memory of one.

Every test drives a real loopback server on port 0. No network, no mocked urllib: the script's
interesting code is the timeout, the status comparison and the unreachable host, and a fake would
skip all three.
"""

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import probe_health as ph  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    """Serves the status its server was told to serve, after an optional delay."""

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler's name)
        status, delay = self.server.plan.get(self.path, (404, 0.0))
        if delay:
            time.sleep(delay)
        body = json.dumps({"path": self.path}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a) -> None:
        return


def serve(plan: dict[str, tuple[int, float]]) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.plan = plan
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TargetParsingTest(unittest.TestCase):
    def test_parses_a_health_line_from_the_coordinator_block(self):
        text = (
            "# Workspace\n\n## Coordinator\n\n"
            "- Timezone: America/Chicago\n"
            "- Health: agent https://example.test/ready 200\n"
            "- Health: login https://example.test/login 200\n"
            "- Board: none\n\n"
            "## Other\n\n- Health: not-a-target https://example.test/x 200\n"
        )
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "CLAUDE.md"
            p.write_text(text)
            targets = ph.targets_from_config(p)
        self.assertEqual(
            [(t.name, t.url, t.expect) for t in targets],
            [("agent", "https://example.test/ready", 200), ("login", "https://example.test/login", 200)],
        )

    def test_missing_block_is_no_targets_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "CLAUDE.md"
            p.write_text("# Workspace\n\nNothing here.\n")
            self.assertEqual(ph.targets_from_config(p), [])

    def test_expected_status_defaults_to_200(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "CLAUDE.md"
            p.write_text("## Coordinator\n\n- Health: agent https://example.test/ready\n")
            self.assertEqual(ph.targets_from_config(p)[0].expect, 200)

    def test_refuses_a_non_http_scheme(self):
        with self.assertRaises(ph.TargetError):
            ph.parse_target("secrets file:///etc/passwd 200")


class ProbeTest(unittest.TestCase):
    def test_matching_status_is_ok(self):
        server, base = serve({"/ready": (200, 0.0)})
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        result = ph.probe(ph.Target("agent", f"{base}/ready", 200))
        self.assertTrue(result.ok)
        self.assertEqual(result.status, 200)

    def test_wrong_status_is_not_ok_and_keeps_the_code(self):
        server, base = serve({"/login": (500, 0.0)})
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        result = ph.probe(ph.Target("login", f"{base}/login", 200))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 500)

    def test_a_404_is_a_status_not_an_exception(self):
        server, base = serve({})
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        result = ph.probe(ph.Target("gone", f"{base}/nope", 200))
        self.assertEqual(result.status, 404)
        self.assertFalse(result.ok)

    def test_unreachable_host_reports_and_does_not_raise(self):
        result = ph.probe(ph.Target("dead", f"http://127.0.0.1:{free_port()}/ready", 200), timeout=1.0)
        self.assertFalse(result.ok)
        self.assertIsNone(result.status)
        self.assertTrue(result.detail)

    def test_a_slow_target_times_out_within_the_budget(self):
        server, base = serve({"/slow": (200, 2.0)})
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        started = time.monotonic()
        result = ph.probe(ph.Target("slow", f"{base}/slow", 200), timeout=0.3)
        self.assertLess(time.monotonic() - started, 1.5, "a hung probe must not stall the move")
        self.assertFalse(result.ok)
        self.assertIsNone(result.status)


class OutputTest(unittest.TestCase):
    def test_one_line_per_target_including_failures(self):
        server, base = serve({"/ready": (200, 0.0), "/login": (500, 0.0)})
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        targets = [
            ph.Target("agent", f"{base}/ready", 200),
            ph.Target("login", f"{base}/login", 200),
            ph.Target("dead", f"http://127.0.0.1:{free_port()}/x", 200),
        ]
        lines, bad = ph.report(targets, timeout=1.0)
        self.assertEqual(len(lines), 3)
        self.assertEqual(bad, 2)
        self.assertRegex(lines[0], r"^agent \S+/ready 200 ok$")
        self.assertIn("login 500", lines[1])
        self.assertIn("expected 200", lines[1])
        self.assertIn("dead", lines[2])

    def test_lines_carry_no_url_query_or_credentials(self):
        server, base = serve({"/ready": (200, 0.0)})
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        lines, _ = ph.report([ph.Target("agent", f"{base}/ready?token=secret", 200)], timeout=1.0)
        self.assertNotIn("secret", lines[0])

    def test_exit_code_is_the_number_of_failures(self):
        server, base = serve({"/ready": (200, 0.0), "/login": (500, 0.0)})
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "CLAUDE.md"
            p.write_text(
                "## Coordinator\n\n"
                f"- Health: agent {base}/ready 200\n"
                f"- Health: login {base}/login 200\n"
            )
            code = ph.main(["--config", str(p), "--timeout", "1"])
        self.assertEqual(code, 1)

    def test_no_targets_exits_zero_and_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "CLAUDE.md"
            p.write_text("## Coordinator\n\n- Board: none\n")
            code = ph.main(["--config", str(p)])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
