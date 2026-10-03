"""Unit tests for scripts/probe_health.py: health facts come from a probe, never from the agent's memory of one.

Every test drives a real loopback server on port 0. No network, no mocked urllib: the script's
interesting code is the timeout, the status comparison and the unreachable host, and a fake would
skip all three.
"""

import contextlib
import io
import json
import os
import re
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
import workspace  # noqa: E402


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


TOML = "chief-of-stuff.toml"
BAD_MODE = '[workers]\nmode = "sometimes"\n'
GOOD = '[workers]\nmode = "one-shot"\n'
# The four lines `workspace.parse_coordinator` refuses a block without.
FULL = "- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n- Timezone: UTC\n"
# Ways a block can spell a `Settings:` line. What each one names is `workspace.read_config`'s to say, not this table's.
FORMS = {
    "one line": "- Settings: `a.toml`\n",
    "two lines": "- Settings: `a.toml`\n- Settings: `b.toml`\n",
    "two lines, the other order": "- Settings: `b.toml`\n- Settings: `a.toml`\n",
    "nested under another bullet": "- Board: none\n  - Settings: `a.toml`\n",
    "a star bullet": "* Settings: `a.toml`\n",
    "no bullet": "Settings: `a.toml`\n",
    "a bullet indented by one space": " - Settings: `a.toml`\n",
    "a backticked label": "- `Settings`: `a.toml`\n",
    "a space before the colon": "- Settings : a.toml\n",
    "an empty value, then a Health: line": "- Settings:\n- Health: agent http://127.0.0.1:{port}/ready 200\n",
}


class SettingsCheckTest(unittest.TestCase):
    """#47: `health` loads the file the `Settings:` line names through the settings loader.

    The blocks stay as small as the ones above: health reads `Settings:` and `Health:` and asks the
    block for nothing else.
    """

    def run_health(self, block: str, toml: str | bytes | None = None, *, text: str | None = None, setup=None) -> tuple[int, list[str], str]:
        """Exit code, stdout lines and stderr of one `health` run over a workspace holding `block`.

        `text` replaces the whole CLAUDE.md; `setup(root)` runs once the files are written, before health does.
        """
        with tempfile.TemporaryDirectory() as d:
            config = Path(d) / "CLAUDE.md"
            config.write_text("## Coordinator\n\n" + block if text is None else text)
            if toml is not None:
                (Path(d) / TOML).write_bytes(toml.encode() if isinstance(toml, str) else toml)
            if setup:
                setup(Path(d))
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ph.main(["--config", str(config), "--timeout", "1"])
        return code, out.getvalue().splitlines(), err.getvalue()

    def targets(self) -> str:
        """Two `Health:` lines, one target up and one answering 500."""
        server, base = serve({"/ready": (200, 0.0), "/login": (500, 0.0)})
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"- Health: agent {base}/ready 200\n- Health: login {base}/login 200\n"

    def settings_line(self, lines: list[str]) -> str:
        found = [x for x in lines if x.startswith("settings")]
        self.assertEqual(len(found), 1, f"one settings line on stdout, got {lines!r}")
        self.assertIn(TOML, found[0])
        return found[0]

    def test_invalid_workers_mode_is_named_on_the_settings_line_and_exits_1(self):
        for spelling in (f"`{TOML}`", TOML, f"`{TOML}` (lanes and workers)"):
            with self.subTest(spelling=spelling):
                code, lines, _ = self.run_health(f"- Settings: {spelling}\n", BAD_MODE)
                self.assertIn("[workers] mode", self.settings_line(lines))
                self.assertEqual(code, 1)

    def test_valid_settings_print_a_settings_ok_line_and_exit_0(self):
        code, lines, _ = self.run_health(f"- Settings: `{TOML}`\n", GOOD)
        self.assertRegex(self.settings_line(lines), r"\bok\b")
        self.assertEqual(code, 0)

    def test_a_settings_fault_counts_as_one_more_failed_check(self):
        code, _, _ = self.run_health(f"- Settings: `{TOML}`\n" + self.targets(), BAD_MODE)
        self.assertEqual(code, 2, "one failing target plus the settings fault")

    def test_a_settings_fault_does_not_hide_or_change_the_target_lines(self):
        targets = self.targets()
        _, alone, _ = self.run_health(targets)
        _, lines, _ = self.run_health(f"- Settings: `{TOML}`\n" + targets, BAD_MODE)
        self.settings_line(lines)
        self.assertEqual([x for x in lines if not x.startswith("settings")], alone)

    def test_valid_settings_leave_target_lines_and_exit_code_unchanged(self):
        targets = self.targets()
        alone_code, alone, _ = self.run_health(targets)
        code, lines, _ = self.run_health(f"- Settings: `{TOML}`\n" + targets, GOOD)
        self.settings_line(lines)
        self.assertEqual([x for x in lines if not x.startswith("settings")], alone)
        self.assertEqual(code, alone_code)

    def test_the_settings_line_comes_before_the_target_lines(self):
        for toml in (GOOD, BAD_MODE):
            with self.subTest(toml=toml):
                # The Settings: line is last in the block: the order is the output's, not the file's.
                _, lines, _ = self.run_health(self.targets() + f"- Settings: `{TOML}`\n", toml)
                self.assertEqual(lines.index(self.settings_line(lines)), 0)

    def test_no_settings_line_prints_no_settings_line(self):
        # The file is there and broken: without a `Settings:` line nothing names it, so nothing reads it.
        code, lines, _ = self.run_health(self.targets(), BAD_MODE)
        self.assertEqual([x for x in lines if x.startswith("settings")], [])
        self.assertEqual(code, 1)

    def test_a_missing_settings_file_is_not_a_fault_and_is_said_to_be_absent(self):
        # settings.load: a `Settings:` line whose path is not a file is the defaults. Nothing was validated,
        # so the line says the file is absent and never says ok.
        for what, setup in (("no file", None), ("a directory in its place", lambda root: (root / TOML).mkdir())):
            with self.subTest(what):
                code, lines, err = self.run_health(f"- Settings: `{TOML}`\n", setup=setup)
                found = self.settings_line(lines)
                self.assertIn("absent", found)
                self.assertNotRegex(found, r"\bok\b")
                self.assertEqual(code, 0)
                self.assertEqual(err, "")

    def test_health_reports_on_the_file_every_other_command_loads(self):
        """Every other command takes the path from `workspace.read_config(root).settings_path`: health names
        that file and no other, and prints no settings line when there is none."""
        for form, lines_ in FORMS.items():
            with self.subTest(form):
                named = []

                def setup(root: Path) -> None:
                    for name in ("a.toml", "b.toml"):
                        (root / name).write_text(GOOD)
                    named.append(workspace.read_config(root).settings_path)

                text = "# Workspace\n\n## Coordinator\n\n" + FULL + lines_.format(port=free_port())
                _, lines, _ = self.run_health("", text=text, setup=setup)
                found = [x for x in lines if x.startswith("settings")]
                if named[0] is None:
                    self.assertEqual(found, [])
                else:
                    self.assertEqual(len(found), 1, found)
                    self.assertEqual(re.findall(r"\b[ab]\.toml\b", found[0]), [named[0]])

    def test_a_settings_line_outside_the_coordinator_block_is_not_read(self):
        stray = f"- Settings: `{TOML}`\n"
        block = "## Coordinator\n\n" + FULL
        for where, text in (("under a heading before the block", f"# Workspace\n\n## Notes\n\n{stray}\n{block}"),
                            ("under a heading after the block", f"# Workspace\n\n{block}\n## Notes\n\n{stray}"),
                            ("above every heading", f"{stray}\n{block}")):
            with self.subTest(where):
                code, lines, _ = self.run_health("", BAD_MODE, text=text)
                self.assertEqual([x for x in lines if x.startswith("settings")], [])
                self.assertEqual(code, 0)

    def test_an_unreadable_settings_file_is_a_settings_fault_line_not_a_traceback(self):
        code, lines, _ = self.run_health(f"- Settings: `{TOML}`\n", GOOD, setup=lambda root: (root / TOML).chmod(0))
        self.assertNotRegex(self.settings_line(lines), r"\bok\b")
        self.assertEqual(code, 1)

    def test_the_no_targets_line_still_prints_after_a_settings_line(self):
        _, alone, _ = self.run_health("- Board: none\n")
        self.assertTrue(alone, "a block with no targets says so")
        for toml in (GOOD, BAD_MODE):
            with self.subTest(toml=toml):
                _, lines, _ = self.run_health(f"- Settings: `{TOML}`\n- Board: none\n", toml)
                self.assertEqual(lines.index(self.settings_line(lines)), 0)
                self.assertEqual(lines[1:], alone)

    def test_a_loader_message_with_a_newline_is_still_one_line(self):
        """One line per check: a key the user wrote reaches the loader's message, and a newline in it must
        not start a second stdout line that reads as a target's."""
        forged = "agent http://127.0.0.1/ready 200 ok"
        _, alone, _ = self.run_health("- Board: none\n")
        for toml in (f'[models."x\\n{forged}"]\nrotation = []\n', f'[lanes."x\\n{forged}"]\nstages = []\n',
                     f'[models."x\\r{forged}"]\nrotation = []\n'):
            with self.subTest(toml=toml):
                code, lines, _ = self.run_health(f"- Settings: `{TOML}`\n- Board: none\n", toml)
                self.settings_line(lines)
                self.assertEqual([x for x in lines if not x.startswith("settings")], alone)
                self.assertEqual(code, 1)

    def test_a_fault_line_names_the_file_once(self):
        # settings.load prefixes these four messages with the path it was given.
        faults = {"invalid TOML": "[workers\nmode = \n", "[lanes] not a table": "lanes = 1\n",
                  "[budgets] not a table of sizes": "budgets = 1\n", "[notify] not a table": "notify = 1\n"}
        for what, toml in faults.items():
            with self.subTest(what):
                code, lines, _ = self.run_health(f"- Settings: `{TOML}`\n", toml)
                self.assertEqual(self.settings_line(lines).count(TOML), 1)
                self.assertEqual(code, 1)

    def test_invalid_toml_is_a_settings_fault_line_not_a_traceback(self):
        code, lines, _ = self.run_health(f"- Settings: `{TOML}`\n", "[workers\nmode = \n")
        self.settings_line(lines)
        self.assertEqual(code, 1)

    def test_an_undecodable_settings_file_is_a_settings_fault_line_not_a_traceback(self):
        code, lines, _ = self.run_health(f"- Settings: `{TOML}`\n", b"[workers]\nmode = \"\xff\xfe\"\n")
        self.settings_line(lines)
        self.assertEqual(code, 1)

    def test_a_malformed_health_target_still_exits_2_on_stderr(self):
        code, _, err = self.run_health("- Health: secrets file:///etc/passwd 200\n")
        self.assertEqual(code, 2)
        self.assertTrue(err.strip())

    def test_a_missing_config_still_exits_2_on_stderr(self):
        with tempfile.TemporaryDirectory() as d:
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = ph.main(["--config", str(Path(d) / "CLAUDE.md")])
        self.assertEqual(code, 2)
        self.assertTrue(err.getvalue().strip())

    def test_a_malformed_health_target_does_not_hide_the_settings_fault(self):
        block = f"- Settings: `{TOML}`\n- Health: secrets file:///etc/passwd 200\n"
        code, lines, err = self.run_health(block, BAD_MODE)
        self.assertIn("[workers] mode", self.settings_line(lines))
        self.assertEqual(code, 2)
        self.assertTrue(err.strip())


if __name__ == "__main__":
    unittest.main()
