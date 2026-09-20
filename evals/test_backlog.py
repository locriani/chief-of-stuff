"""Unit tests for scripts/backlog.py: the backlog lives in GitLab, and a missing answer is never a clean one.

Every test drives a real loopback server on port 0, the way test_probe_health.py does. The
interesting code is the header, the pagination and the four ways a fetch can fail, and a mocked
urllib would skip all four. The suite never touches the network and never reads the real Keychain:
the Keychain lookup is injected, because a test that passes only on Zach's laptop proves nothing.
"""

import json
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import backlog as bl  # noqa: E402

TOKEN = "glpat-test-not-a-real-token"


class Handler(BaseHTTPRequestHandler):
    """Serves the response its server was told to serve for (path, state, page), and records the ask."""

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler's name)
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        key = (parts.path, query.get("state", [""])[0], query.get("page", ["1"])[0])
        # The Message itself, not a dict of it: header names are case-insensitive and urllib
        # sends `Private-Token`. A dict lookup would fail a request GitLab accepts.
        self.server.seen.append((self.path, self.headers))
        status, body, headers = self.server.plan.get(key, (404, '{"message":"404 Not found"}', {}))
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_a) -> None:
        return


def serve(plan: dict) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.plan = plan
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def stop(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def issue(iid: int, title: str, state: str = "opened", labels=()) -> dict:
    return {
        "iid": iid,
        "title": title,
        "state": state,
        "labels": list(labels),
        "web_url": f"https://labs.gauntletai.com/zachgardner/openemr/-/issues/{iid}",
        "updated_at": "2026-09-20T05:11:02.000Z",
    }


ISSUES = "/api/v4/projects/zachgardner%2Fopenemr/issues"


def config_text(line: str) -> str:
    return (
        "# Workspace\n\n## Coordinator\n\n"
        "- User: Zach\n"
        "- Timezone: America/Chicago\n"
        f"{line}"
        "- Board: Artifact tool; URL https://example.test/b\n\n"
        "## After\n"
    )


def written(text: str) -> Path:
    path = Path(tempfile.mkdtemp()) / "CLAUDE.md"
    path.write_text(text)
    return path


def cfg(base: str) -> bl.Backlog:
    return bl.Backlog(host=base, project="zachgardner/openemr", env="CHIEF_OF_STUFF_GITLAB_TOKEN")


class ConfigTest(unittest.TestCase):
    def test_reads_host_project_and_env_name_from_the_coordinator_block(self):
        path = written(config_text(
            "- Backlog: GitLab; host https://labs.gauntletai.com; project zachgardner/openemr;"
            " token env CHIEF_OF_STUFF_GITLAB_TOKEN\n"
        ))
        found = bl.backlog_from_config(path)
        self.assertEqual(found.host, "https://labs.gauntletai.com")
        self.assertEqual(found.project, "zachgardner/openemr")
        self.assertEqual(found.env, "CHIEF_OF_STUFF_GITLAB_TOKEN")

    def test_no_backlog_line_is_absence_not_an_error(self):
        """A workspace with no backlog is a workspace with no backlog. Absent is a state, not a crash."""
        self.assertIsNone(bl.backlog_from_config(written(config_text(""))))

    def test_a_host_that_is_not_http_is_refused(self):
        path = written(config_text("- Backlog: GitLab; host ssh://labs.gauntletai.com; project a/b\n"))
        with self.assertRaises(bl.BacklogError):
            bl.backlog_from_config(path)

    def test_a_literal_token_in_the_config_is_refused(self):
        """House rule 6: a token never lives in a file that is committed. The config carries its NAME."""
        path = written(config_text(
            f"- Backlog: GitLab; host https://labs.gauntletai.com; project a/b; token {TOKEN}\n"
        ))
        with self.assertRaises(bl.BacklogError) as caught:
            bl.backlog_from_config(path)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_keychain_service_is_derived_from_the_host(self):
        self.assertEqual(cfg("https://labs.gauntletai.com").service, "gitlab-labs.gauntletai.com")


class TokenTest(unittest.TestCase):
    def test_environment_wins_over_the_keychain(self):
        got = bl.token(cfg("https://h.test"), environ={"CHIEF_OF_STUFF_GITLAB_TOKEN": "from-env"},
                       keychain=lambda *_a: "from-keychain")
        self.assertEqual(got, "from-env")

    def test_keychain_is_not_consulted_when_the_environment_has_it(self):
        asked = []
        bl.token(cfg("https://h.test"), environ={"CHIEF_OF_STUFF_GITLAB_TOKEN": "from-env"},
                 keychain=lambda *a: asked.append(a) or "")
        self.assertEqual(asked, [])

    def test_keychain_answers_when_the_environment_does_not(self):
        got = bl.token(cfg("https://h.test"), environ={}, keychain=lambda account, service: "from-keychain")
        self.assertEqual(got, "from-keychain")

    def test_absent_everywhere_is_the_empty_string_not_an_exception(self):
        self.assertEqual(bl.token(cfg("https://h.test"), environ={}, keychain=lambda *_a: ""), "")

    def test_a_keychain_that_fails_is_absence_not_a_crash(self):
        def boom(*_a):
            raise OSError("security: not found")
        self.assertEqual(bl.token(cfg("https://h.test"), environ={}, keychain=boom), "")


class IssuesTest(unittest.TestCase):
    def test_reads_one_page_and_parses_the_fields(self):
        body = json.dumps([issue(3, "PHQ-9-teen wired dead", labels=["defect"])])
        server, base = serve({(ISSUES, "opened", "1"): (200, body, {})})
        self.addCleanup(stop, server)
        got = bl.issues(cfg(base), token=TOKEN)
        self.assertEqual(got.error, "")
        self.assertEqual(len(got.issues), 1)
        one = got.issues[0]
        self.assertEqual((one.iid, one.title, one.state), (3, "PHQ-9-teen wired dead", "opened"))
        self.assertEqual(one.labels, ("defect",))

    def test_sends_the_token_as_a_private_token_header(self):
        server, base = serve({(ISSUES, "opened", "1"): (200, "[]", {})})
        self.addCleanup(stop, server)
        bl.issues(cfg(base), token=TOKEN)
        _path, headers = server.seen[0]
        self.assertEqual(headers.get("PRIVATE-TOKEN"), TOKEN)

    def test_follows_x_next_page_to_the_end(self):
        """A page cap that silently truncates would read as a shorter backlog, which is 116(a) again."""
        plan = {
            (ISSUES, "opened", "1"): (200, json.dumps([issue(1, "one")]), {"X-Next-Page": "2"}),
            (ISSUES, "opened", "2"): (200, json.dumps([issue(2, "two")]), {"X-Next-Page": ""}),
        }
        server, base = serve(plan)
        self.addCleanup(stop, server)
        got = bl.issues(cfg(base), token=TOKEN)
        self.assertEqual([i.iid for i in got.issues], [1, 2])

    def test_no_token_never_makes_a_request(self):
        server, base = serve({})
        self.addCleanup(stop, server)
        got = bl.issues(cfg(base), token="")
        self.assertIn("no token", got.error)
        self.assertEqual(got.issues, ())
        self.assertEqual(server.seen, [])

    def test_an_http_error_is_a_value_not_an_exception(self):
        server, base = serve({(ISSUES, "opened", "1"): (401, '{"message":"401 Unauthorized"}', {})})
        self.addCleanup(stop, server)
        got = bl.issues(cfg(base), token=TOKEN)
        self.assertIn("401", got.error)
        self.assertEqual(got.issues, ())

    def test_malformed_json_is_a_value_not_an_exception(self):
        server, base = serve({(ISSUES, "opened", "1"): (200, "not json", {})})
        self.addCleanup(stop, server)
        got = bl.issues(cfg(base), token=TOKEN)
        self.assertTrue(got.error)
        self.assertEqual(got.issues, ())

    def test_an_unreachable_host_is_a_value_not_an_exception(self):
        got = bl.issues(cfg(f"http://127.0.0.1:{free_port()}"), token=TOKEN, timeout=0.5)
        self.assertTrue(got.error)
        self.assertEqual(got.issues, ())

    def test_labels_narrow_the_ask(self):
        server, base = serve({(ISSUES, "opened", "1"): (200, "[]", {})})
        self.addCleanup(stop, server)
        bl.issues(cfg(base), token=TOKEN, labels=("agent-layer", "defect"))
        self.assertIn("labels=agent-layer%2Cdefect", server.seen[0][0])


class CountsTest(unittest.TestCase):
    def test_counts_open_and_closed(self):
        plan = {
            (ISSUES, "opened", "1"): (200, "[]", {"X-Total": "93"}),
            (ISSUES, "closed", "1"): (200, "[]", {"X-Total": "7"}),
        }
        server, base = serve(plan)
        self.addCleanup(stop, server)
        got = bl.counts(cfg(base), token=TOKEN)
        self.assertEqual((got.open, got.closed, got.error), (93, 7, ""))
        self.assertEqual(bl.line(got), "backlog: 93 open, 7 closed")

    def test_a_missing_answer_never_renders_as_a_clean_zero(self):
        """116(a)'s rule reaches here: `0 open` and `we could not ask` must not read the same."""
        got = bl.counts(cfg(f"http://127.0.0.1:{free_port()}"), token=TOKEN, timeout=0.5)
        self.assertTrue(got.error)
        self.assertIsNone(got.open)
        rendered = bl.line(got)
        self.assertIn("unknown", rendered)
        self.assertNotIn("0 open", rendered)

    def test_no_token_is_unknown_not_empty(self):
        server, base = serve({})
        self.addCleanup(stop, server)
        rendered = bl.line(bl.counts(cfg(base), token=""))
        self.assertIn("unknown", rendered)
        self.assertNotIn("0 open", rendered)


if __name__ == "__main__":
    unittest.main()
