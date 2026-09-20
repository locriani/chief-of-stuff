"""Unit tests for scripts/backlog.py: the backlog lives in GitLab, and a missing answer is never a clean one.

Every test drives a real loopback server on port 0, the way test_probe_health.py does. The
interesting code is the header, the pagination and the four ways a fetch can fail, and a mocked
urllib would skip all four. The suite never touches the network and never reads the real Keychain:
the Keychain lookup is injected, because a test that passes only on Zach's laptop proves nothing.
"""

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import backlog as bl  # noqa: E402

TOKEN = "glpat-test-not-a-real-token"


class Ask(NamedTuple):
    method: str
    path: str
    headers: object
    body: dict


class Handler(BaseHTTPRequestHandler):
    """Serves what its server was told to serve for (method, path, state, page), and records the ask."""

    def _handle(self, method: str) -> None:
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        key = (method, parts.path, query.get("state", [""])[0], query.get("page", ["1"])[0])
        length = int(self.headers.get("Content-Length") or 0)
        raw_in = self.rfile.read(length) if length else b""
        try:
            sent = json.loads(raw_in) if raw_in else {}
        except ValueError:
            sent = {"_raw": raw_in.decode("utf-8", "replace")}
        # The Message itself, not a dict of it: header names are case-insensitive and urllib
        # sends `Private-Token`. A dict lookup would fail a request GitLab accepts.
        self.server.seen.append(Ask(method, self.path, self.headers, sent))
        status, body, headers = self.server.plan.get(key, (404, '{"message":"404 Not found"}', {}))
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler's names)
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

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
        server, base = serve({("GET", ISSUES, "opened", "1"): (200, body, {})})
        self.addCleanup(stop, server)
        got = bl.issues(cfg(base), token=TOKEN)
        self.assertEqual(got.error, "")
        self.assertEqual(len(got.issues), 1)
        one = got.issues[0]
        self.assertEqual((one.iid, one.title, one.state), (3, "PHQ-9-teen wired dead", "opened"))
        self.assertEqual(one.labels, ("defect",))

    def test_sends_the_token_as_a_private_token_header(self):
        server, base = serve({("GET", ISSUES, "opened", "1"): (200, "[]", {})})
        self.addCleanup(stop, server)
        bl.issues(cfg(base), token=TOKEN)
        self.assertEqual(server.seen[0].headers.get("PRIVATE-TOKEN"), TOKEN)

    def test_follows_x_next_page_to_the_end(self):
        """A page cap that silently truncates would read as a shorter backlog, which is 116(a) again."""
        plan = {
            ("GET", ISSUES, "opened", "1"): (200, json.dumps([issue(1, "one")]), {"X-Next-Page": "2"}),
            ("GET", ISSUES, "opened", "2"): (200, json.dumps([issue(2, "two")]), {"X-Next-Page": ""}),
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
        server, base = serve({("GET", ISSUES, "opened", "1"): (401, '{"message":"401 Unauthorized"}', {})})
        self.addCleanup(stop, server)
        got = bl.issues(cfg(base), token=TOKEN)
        self.assertIn("401", got.error)
        self.assertEqual(got.issues, ())

    def test_malformed_json_is_a_value_not_an_exception(self):
        server, base = serve({("GET", ISSUES, "opened", "1"): (200, "not json", {})})
        self.addCleanup(stop, server)
        got = bl.issues(cfg(base), token=TOKEN)
        self.assertTrue(got.error)
        self.assertEqual(got.issues, ())

    def test_an_unreachable_host_is_a_value_not_an_exception(self):
        got = bl.issues(cfg(f"http://127.0.0.1:{free_port()}"), token=TOKEN, timeout=0.5)
        self.assertTrue(got.error)
        self.assertEqual(got.issues, ())

    def test_labels_narrow_the_ask(self):
        server, base = serve({("GET", ISSUES, "opened", "1"): (200, "[]", {})})
        self.addCleanup(stop, server)
        bl.issues(cfg(base), token=TOKEN, labels=("agent-layer", "defect"))
        self.assertIn("labels=agent-layer%2Cdefect", server.seen[0].path)


class CountsTest(unittest.TestCase):
    def test_counts_open_and_closed(self):
        plan = {
            ("GET", ISSUES, "opened", "1"): (200, "[]", {"X-Total": "93"}),
            ("GET", ISSUES, "closed", "1"): (200, "[]", {"X-Total": "7"}),
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


LABELS = "/api/v4/projects/zachgardner%2Fopenemr/labels"


def key(method: str, path: str) -> tuple:
    """A write carries no `state` or `page`, so its plan key is the defaults the handler reads."""
    return (method, path, "", "1")


def made(iid: int, title: str) -> str:
    return json.dumps(issue(iid, title))


class WriteTest(unittest.TestCase):
    def test_dry_run_is_the_default_and_touches_nothing(self):
        """A write is outward-facing. The default has to be the one that cannot do damage."""
        server, base = serve({})
        self.addCleanup(stop, server)
        got = bl.create(cfg(base), "seed data has no cohort", token=TOKEN)
        self.assertEqual(server.seen, [])
        self.assertFalse(got.done)
        self.assertTrue(got.ok)
        self.assertIn("would create", bl.write_line(got))

    def test_commit_posts_the_issue_and_reads_back_what_gitlab_made(self):
        plan = {("GET", LABELS, "", "1"): (200, json.dumps([{"name": "defect"}]), {}),
                key("POST", ISSUES): (201, made(12, "seed data has no cohort"), {})}
        server, base = serve(plan)
        self.addCleanup(stop, server)
        got = bl.create(cfg(base), "seed data has no cohort", body="three rules cannot fire",
                        labels=("defect",), token=TOKEN, commit=True)
        self.assertTrue(got.done, got.error)
        self.assertEqual(got.iid, 12)
        self.assertIn("/-/issues/12", got.url)
        sent = server.seen[-1]
        self.assertEqual(sent.method, "POST")
        self.assertEqual(sent.body["title"], "seed data has no cohort")
        self.assertEqual(sent.body["description"], "three rules cannot fire")
        self.assertEqual(sent.body["labels"], "defect")
        self.assertEqual(sent.headers.get("PRIVATE-TOKEN"), TOKEN)
        self.assertIn("created #12", bl.write_line(got))

    def test_a_create_without_a_token_never_reaches_the_network(self):
        server, base = serve({})
        self.addCleanup(stop, server)
        got = bl.create(cfg(base), "t", token="", commit=True)
        self.assertEqual(server.seen, [])
        self.assertIn("no token", got.error)
        self.assertFalse(got.done)

    def test_a_refused_write_never_reads_as_a_write_that_happened(self):
        """116(a) for the write side: `failed` and `created` must not be the same sentence."""
        server, base = serve({key("POST", ISSUES): (403, '{"message":"403 Forbidden"}', {})})
        self.addCleanup(stop, server)
        got = bl.create(cfg(base), "t", token=TOKEN, commit=True)
        self.assertFalse(got.done)
        self.assertIn("403", got.error)
        rendered = bl.write_line(got)
        self.assertIn("failed", rendered)
        self.assertNotIn("created #", rendered)

    def test_close_sends_the_state_event(self):
        server, base = serve({key("PUT", f"{ISSUES}/12"): (200, made(12, "t"), {})})
        self.addCleanup(stop, server)
        got = bl.close(cfg(base), 12, token=TOKEN, commit=True)
        self.assertTrue(got.done, got.error)
        sent = server.seen[-1]
        self.assertEqual(sent.method, "PUT")
        self.assertEqual(sent.body["state_event"], "close")

    def test_close_dry_run_touches_nothing(self):
        server, base = serve({})
        self.addCleanup(stop, server)
        got = bl.close(cfg(base), 12, token=TOKEN)
        self.assertEqual(server.seen, [])
        self.assertIn("would close", bl.write_line(got))

    def test_a_closed_issue_is_named_once(self):
        """`closed #1: #1` is what naming the issue in both halves of the line looks like."""
        server, base = serve({key("PUT", f"{ISSUES}/12"): (200, made(12, "t"), {})})
        self.addCleanup(stop, server)
        self.assertEqual(bl.write_line(bl.close(cfg(base), 12, token=TOKEN, commit=True)), "closed #12")

    def test_comment_posts_a_note_on_the_issue(self):
        server, base = serve({key("POST", f"{ISSUES}/12/notes"): (201, '{"id": 99}', {})})
        self.addCleanup(stop, server)
        got = bl.comment(cfg(base), 12, "landed on main as 3c8476e", token=TOKEN, commit=True)
        self.assertTrue(got.done, got.error)
        sent = server.seen[-1]
        self.assertEqual((sent.method, sent.body["body"]), ("POST", "landed on main as 3c8476e"))


class LabelTest(unittest.TestCase):
    """Labels are created on demand from what a write asks for. No vocabulary is baked in:
    Zach, 2026-09-19 22:47 — "lanes are also going to be changeable over time"."""

    def test_creates_only_the_labels_that_are_missing(self):
        plan = {
            ("GET", LABELS, "", "1"): (200, json.dumps([{"name": "defect"}]), {}),
            key("POST", LABELS): (201, '{"name": "agent-layer"}', {}),
        }
        server, base = serve(plan)
        self.addCleanup(stop, server)
        got = bl.ensure_labels(cfg(base), ("defect", "agent-layer"), token=TOKEN, commit=True)
        posted = [a for a in server.seen if a.method == "POST"]
        self.assertEqual([a.body["name"] for a in posted], ["agent-layer"])
        self.assertEqual([w.what for w in got if w.done], ["agent-layer"])

    def test_a_label_that_already_exists_is_not_created_again(self):
        plan = {("GET", LABELS, "", "1"): (200, json.dumps([{"name": "defect"}]), {})}
        server, base = serve(plan)
        self.addCleanup(stop, server)
        got = bl.ensure_labels(cfg(base), ("defect",), token=TOKEN, commit=True)
        self.assertEqual([a for a in server.seen if a.method == "POST"], [])
        self.assertEqual(got, ())

    def test_dry_run_creates_no_label(self):
        plan = {("GET", LABELS, "", "1"): (200, "[]", {})}
        server, base = serve(plan)
        self.addCleanup(stop, server)
        got = bl.ensure_labels(cfg(base), ("agent-layer",), token=TOKEN)
        self.assertEqual([a for a in server.seen if a.method == "POST"], [])
        self.assertIn("would create label", bl.write_line(got[0]))

    def test_a_label_list_that_fails_is_a_value_not_an_exception(self):
        server, base = serve({("GET", LABELS, "", "1"): (500, '{"message":"500"}', {})})
        self.addCleanup(stop, server)
        got = bl.ensure_labels(cfg(base), ("agent-layer",), token=TOKEN, commit=True)
        self.assertTrue(got[0].error)
        self.assertFalse(got[0].done)

    def test_create_makes_its_labels_before_it_makes_the_issue(self):
        """An issue POSTed with an unknown label silently drops it on some GitLab versions."""
        plan = {
            ("GET", LABELS, "", "1"): (200, "[]", {}),
            key("POST", LABELS): (201, '{"name": "seed-data"}', {}),
            key("POST", ISSUES): (201, made(13, "t"), {}),
        }
        server, base = serve(plan)
        self.addCleanup(stop, server)
        bl.create(cfg(base), "t", labels=("seed-data",), token=TOKEN, commit=True)
        posts = [a.path.split("?")[0] for a in server.seen if a.method == "POST"]
        self.assertEqual(posts, [LABELS, ISSUES])


class CliWriteTest(unittest.TestCase):
    def config_at(self, base: str) -> str:
        return str(written(config_text(
            f"- Backlog: GitLab; host {base}; project zachgardner/openemr;"
            " token env CHIEF_OF_STUFF_GITLAB_TOKEN\n"
        )))

    def test_create_without_commit_writes_nothing(self):
        """There is deliberately no `--token` flag: argv is readable by `ps`, so the CLI reads the
        environment and the Keychain and nothing else."""
        server, base = serve({})
        self.addCleanup(stop, server)
        with patch.dict(os.environ, {"CHIEF_OF_STUFF_GITLAB_TOKEN": TOKEN}):
            code = bl.main(["--config", self.config_at(base), "--create", "a title"])
        self.assertEqual(code, 0)
        self.assertEqual(server.seen, [])

    def test_create_with_commit_writes(self):
        plan = {("GET", LABELS, "", "1"): (200, "[]", {}),
                key("POST", ISSUES): (201, made(14, "a title"), {})}
        server, base = serve(plan)
        self.addCleanup(stop, server)
        with patch.dict(os.environ, {"CHIEF_OF_STUFF_GITLAB_TOKEN": TOKEN}):
            code = bl.main(["--config", self.config_at(base), "--create", "a title", "--commit"])
        self.assertEqual(code, 0)
        self.assertEqual([a.method for a in server.seen if a.method == "POST"], ["POST"])


if __name__ == "__main__":
    unittest.main()
