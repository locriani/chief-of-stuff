"""pages.py: the workspace's pages, served from this Mac (Zach, 2026-09-24 14:07: "we should host our own
webserver and ensure they are set up as part of the agent's boot loop")."""

import http.client
import json
import http.server
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pages as pg  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(port: int, path: str, host: str | None = None, method: str = "GET") -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, headers={"Host": host or f"127.0.0.1:{port}"})
    resp = conn.getresponse()
    resp.body = resp.read()
    conn.close()
    return resp


def post(port: int, path: str, body: bytes, host: str | None = None, origin: str | None = None) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{port}", "Content-Type": "application/x-www-form-urlencoded"}
    if origin:
        headers["Origin"] = origin
    conn.request("POST", path, body=body, headers=headers)
    resp = conn.getresponse()
    resp.body = resp.read()
    conn.close()
    return resp


def pages_dir() -> Path:
    root = Path(tempfile.mkdtemp())
    for name in ("2026-09-23-board.html", "2026-09-24-board.html", "arch-review.html"):
        (root / name).write_text(f"<p>{name}</p>")
    (root / ".pid").write_text("1")
    return root


class ServeTest(unittest.TestCase):
    def setUp(self):
        self.dir = pages_dir()
        self.server = pg.make_server(self.dir, 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_root_serves_the_newest_board(self):
        # The tab stays on `/`, so its reload poll sees the next day's board, not only today's file.
        self.assertEqual((get(self.port, "/").status, get(self.port, "/").body), (200, b"<p>2026-09-24-board.html</p>"))
        self.assertEqual(get(self.port, "/", method="HEAD").status, 200)
        (self.dir / "2026-09-25-board.html").write_text("<p>next</p>")
        self.assertEqual(get(self.port, "/").body, b"<p>next</p>")

    def test_a_page_is_served_uncached_and_names_the_server(self):
        resp = get(self.port, "/arch-review.html")
        self.assertEqual((resp.status, resp.body), (200, b"<p>arch-review.html</p>"))
        self.assertEqual(resp.getheader("Cache-Control"), "no-cache")
        self.assertTrue(resp.getheader("Server").startswith(pg.SERVER))

    def test_text_is_served_as_utf8(self):
        # A page with no <meta charset> was read as Windows-1252: "Decision · x" showed as "Decision Â· x".
        self.assertEqual(get(self.port, "/arch-review.html").getheader("Content-Type"), "text/html; charset=utf-8")

    def test_all_lists_every_page(self):
        resp = get(self.port, "/all")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"arch-review.html", resp.body)
        # A rendered page is a route, not a file (the user, 2026-09-26: "get rid of the .html versions entirely").
        for hidden in (b".pid", b"-board.html"):
            self.assertNotIn(hidden, resp.body)

    def test_localhost_is_served(self):
        self.assertEqual(get(self.port, "/arch-review.html", host=f"localhost:{self.port}").status, 200)

    def test_another_host_is_refused(self):
        # A page elsewhere can rebind its own name to 127.0.0.1; the Host it sends is still its own.
        self.assertEqual(get(self.port, "/arch-review.html", host="evil.test").status, 403)
        self.assertEqual(get(self.port, "/arch-review.html", host=f"evil.test:{self.port}").status, 403)

    def test_dotfiles_are_not_served(self):
        self.assertEqual(get(self.port, "/.pid").status, 404)


class EnsureTest(unittest.TestCase):
    def setUp(self):
        self.dir = pages_dir()
        (self.dir / ".pid").unlink()
        self.port = free_port()
        self.log = self.dir.parent / f"pages-{self.port}.log"

    def stop(self):
        pid = self.dir / ".pid"
        if pid.is_file():
            os.kill(int(pid.read_text()), signal.SIGTERM)

    def test_starts_once_then_reuses(self):
        self.addCleanup(self.stop)
        first = pg.ensure(self.dir, self.port, self.log)
        self.assertEqual(first, "pages: started")
        pid = (self.dir / ".pid").read_text()
        self.assertEqual(get(self.port, "/").status, 200)
        self.assertEqual(pg.ensure(self.dir, self.port, self.log), "pages: serving")
        self.assertEqual((self.dir / ".pid").read_text(), pid)

    def test_a_denied_connection_is_not_a_free_port(self):
        # A sandbox without network refuses the loopback connect; the server on the port may be running fine.
        denied = urllib.error.URLError(PermissionError(1, "Operation not permitted"))
        with mock.patch.object(pg.urllib.request, "urlopen", side_effect=denied), \
                mock.patch.object(pg.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(pg.PagesError, f"{self.port}.*denied"):
                pg.ensure(self.dir, self.port, self.log)
        popen.assert_not_called()

    def test_a_server_that_never_came_up_leaves_no_pid(self):
        # A dead child's pid in .pid made the next ensure refuse to stop the real server (#248).
        dead = mock.Mock(pid=4242, **{"poll.return_value": 1})
        with mock.patch.object(pg.subprocess, "Popen", return_value=dead):
            with self.assertRaisesRegex(pg.PagesError, "did not come up"):
                pg.ensure(self.dir, self.port, self.log)
        self.assertFalse((self.dir / ".pid").exists())

    def test_a_foreign_server_on_the_port_is_an_error(self):
        other = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        with self.assertRaisesRegex(pg.PagesError, f"{self.port}.*not the pages server"):
            pg.ensure(self.dir, self.port, self.log)

    def test_an_older_server_is_restarted(self):
        # A server started before `--root` existed never renders; `--ensure` replaces it, by its pid file.
        self.addCleanup(self.stop)
        old = start_old_server(self.dir, self.port)
        self.addCleanup(old.kill)
        (self.dir / ".pid").write_text(str(old.pid))
        self.assertEqual(pg.ensure(self.dir, self.port, self.log), "pages: restarted")
        self.assertIsNotNone(old.wait(timeout=5))
        self.assertIn(f"{pg.SERVER}/{pg.VERSION}", pg._who(self.port))

    def test_an_older_server_is_stopped_only_by_its_own_pid(self):
        old = start_old_server(self.dir, self.port)
        self.addCleanup(old.kill)
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(other.kill)
        (self.dir / ".pid").write_text(str(other.pid))
        with self.assertRaisesRegex(pg.PagesError, "not it"):
            pg.ensure(self.dir, self.port, self.log)
        self.assertIsNone(other.poll())
        self.assertIsNone(old.poll())


OLD_SERVER = """import functools, http.server, sys
class H(http.server.SimpleHTTPRequestHandler):
    server_version = "chief-of-stuff-pages"
http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[5])), functools.partial(H, directory=sys.argv[3])).serve_forever()
"""


def start_old_server(pages: Path, port: int) -> subprocess.Popen:
    script = Path(tempfile.mkdtemp()) / "pages.py"
    script.write_text(OLD_SERVER)
    proc = subprocess.Popen([sys.executable, str(script), "serve", "--dir", str(pages), "--port", str(port)],
                            stderr=subprocess.DEVNULL)
    for _ in range(50):
        if pg._who(port) is not None:
            return proc
        time.sleep(0.1)
    raise AssertionError("old server did not start")


ZONE = "America/Chicago"
TRACKER = """# Tracker {day}

## Tasks

| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
| {task} | Robin | open | 09:00 |  | Checklist: {task} |

## Decisions

| time | item | Robin's words |
|---|---|---|

## Log

- 09:00 opened the day
"""


class RenderOnRequestTest(unittest.TestCase):
    """The agent never renders: a GET re-renders the page its sources outdate (the user, 2026-09-26: "I want to
    make the page render automatically using scripts and stuff instead of the agent pulling in data each time")."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.day = datetime.now(ZoneInfo(ZONE)).date().isoformat()
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            f"- Timezone: {ZONE}\n- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n"
            "- Settings: `cos.toml`\n")
        (self.root / "daily").mkdir()
        self.tracker = self.root / "daily" / f"{self.day}-tracker.md"
        self.tracker.write_text(TRACKER.format(day=self.day, task="Security audit"))
        self.pages = self.root / "pages"
        self.pages.mkdir()
        if getattr(self, "SETTINGS", ""):
            (self.root / "cos.toml").write_text(self.SETTINGS)
        self.calls, self.refreshed = [], threading.Event()
        self.server = pg.make_server(self.pages, 0, root=self.root, refresh=self.fake_refresh, every=3600)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        # The first refresh writes .sources.json, a page source; a test racing it sees a re-render it didn't cause.
        self.assertTrue(self.refreshed.wait(10))

    def fake_refresh(self, root, now):
        self.calls.append(now)
        import board_sources
        try:
            return board_sources.refresh(root, now)
        finally:
            self.refreshed.set()

    def board(self) -> Path:
        return self.pages / f"{self.day}-board.html"

    def test_root_renders_todays_board_and_rerenders_after_a_tracker_edit(self):
        self.assertIn(b"Security audit", get(self.port, "/").body)
        self.tracker.write_text(TRACKER.format(day=self.day, task="Draft release notes"))
        self.assertIn(b"Draft release notes", get(self.port, "/").body)
        self.assertTrue((self.pages / "decisions.html").is_file())

    def test_an_unchanged_page_is_served_as_written(self):
        get(self.port, "/")
        before = self.board().stat().st_mtime_ns
        resp = get(self.port, "/", method="HEAD")
        self.assertEqual((resp.status, self.board().stat().st_mtime_ns), (200, before))

    def test_a_render_error_keeps_the_last_good_page_with_a_banner(self):
        get(self.port, "/")
        (self.root / "cos.toml").write_text("[lanes\n")
        resp = get(self.port, "/")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"Security audit", resp.body)
        self.assertIn(b"not re-rendered", resp.body)
        self.assertIn(b'role="alert"', resp.body)

    def test_a_decision_page_renders_from_its_json(self):
        (self.pages / "decision-cache-ttl.json").write_text(
            '{"headline": "Cache TTL", "ask": "Keep 5 minutes?", "options": [{"key": "A", "title": "Keep", '
            '"text": "no change"}, {"key": "B", "title": "Drop", "text": "slower"}], "recommended": "A", "why": "fine", "default": "A at 17:00"}')
        resp = get(self.port, "/decisions/cache-ttl")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"Cache TTL", resp.body)
        self.assertIn(b"Cache TTL", get(self.port, "/decisions").body)

    def test_pages_are_routes_and_their_files_are_not_served(self):
        # The user, 2026-09-26: "pages should be: / /decisions /decisions/id actually an app".
        (self.pages / "decision-cache-ttl.json").write_text(
            '{"headline": "Cache TTL", "ask": "Keep 5 minutes?", "options": [{"key": "A", "title": "Keep", '
            '"text": "no change"}, {"key": "B", "title": "Drop", "text": "slower"}], "recommended": "A", "why": "fine", "default": "A at 17:00"}')
        page = get(self.port, "/decisions/cache-ttl")
        self.assertEqual(page.status, 200)
        self.assertIn(b"Cache TTL", page.body)
        self.assertIn(b'href="/decisions"', page.body)
        self.assertIn(b'href="/decisions/cache-ttl"', get(self.port, "/decisions").body)
        # The user, 2026-09-26: "get rid of the .html versions entirely".
        for missing in ("/decisions/nope", "/decisions/../decisions", "/decisions/cache-ttl.json", "/decisions.html",
                        "/decision-cache-ttl.html", f"/{self.day}-board.html"):
            with self.subTest(missing=missing):
                self.assertEqual(get(self.port, missing).status, 404)

    def test_a_page_rendered_by_an_older_release_is_rendered_again(self):
        # A new release changes the renderer, not the sources: every page written before it is out of date.
        (self.pages / "decision-cache-ttl.json").write_text(
            '{"headline": "Cache TTL", "ask": "Keep 5 minutes?", "options": [{"key": "A", "title": "Keep", '
            '"text": "no change"}, {"key": "B", "title": "Drop", "text": "slower"}], "recommended": "A", "why": "fine", "default": "A at 17:00"}')
        self.assertEqual(get(self.port, "/decisions/cache-ttl").status, 200)
        get(self.port, "/")
        old = 1_000_000_000  # 2001: newer than no source, older than any release
        for f in [*self.pages.glob("*.json"), self.tracker, self.root / "CLAUDE.md"]:
            os.utime(f, (old - 10, old - 10))
        for f in self.pages.glob("*.html"):
            f.write_text("rendered by an older release")
            os.utime(f, (old, old))
        for path in ("/decisions/cache-ttl", "/decisions", "/"):
            with self.subTest(path=path):
                self.assertNotIn(b"rendered by an older release", get(self.port, path).body)

    def test_dotfiles_and_foreign_hosts_are_still_refused(self):
        self.assertEqual(get(self.port, "/.sources.json").status, 404)
        self.assertEqual(get(self.port, "/", host="evil.test").status, 403)

    def test_the_refresher_writes_the_cache_and_a_stale_cache_wakes_it(self):
        cache = self.pages / ".sources.json"
        for _ in range(50):
            if cache.is_file():
                break
            time.sleep(0.05)
        self.assertTrue(cache.is_file())
        self.assertEqual(len(self.calls), 1)
        old = time.time() - 7200
        os.utime(cache, (old, old))
        get(self.port, "/")
        for _ in range(50):
            if len(self.calls) > 1:
                break
            time.sleep(0.05)
        self.assertEqual(len(self.calls), 2)


class AnswerTest(unittest.TestCase):
    """The user answers on the page; the server saves it beside the decision for the coordinator's next pass (the user,
    2026-09-26: "Selecting / entering an option should save out that decision so the agent can pick it up next pass")."""

    fake_refresh = RenderOnRequestTest.fake_refresh

    def setUp(self):
        RenderOnRequestTest.setUp(self)
        self.json = self.pages / "decision-cache-ttl.json"
        self.json.write_text(json.dumps({"headline": "Cache TTL", "ask": "Keep 5 minutes?", "options": [
            {"key": "A", "title": "Keep", "text": "no change"}, {"key": "B", "title": "Drop", "text": "slower"}],
            "recommended": "A", "why": "fine", "default": "A at 17:00"}))
        old = time.time() - 3600
        os.utime(self.json, (old, old))
        self.origin = f"http://127.0.0.1:{self.port}"

    def answer(self) -> dict | None:
        return json.loads(self.json.read_text()).get("answer")

    def test_a_choice_is_saved_and_the_page_shows_it_answered(self):
        mtime = self.json.stat().st_mtime_ns
        get(self.port, "/decisions/cache-ttl")
        resp = post(self.port, "/decisions/cache-ttl", b"key=B&words=", origin=self.origin)
        self.assertEqual((resp.status, resp.getheader("Location")), (303, "/decisions/cache-ttl"))
        saved = self.answer()
        self.assertEqual((saved["key"], saved["words"]), ("B", ""))
        self.assertLess(abs(datetime.fromisoformat(saved["at"]).timestamp() - time.time()), 60)
        self.assertEqual(self.json.stat().st_mtime_ns, mtime)  # the file's time stays when it was asked
        page = get(self.port, "/decisions/cache-ttl").body.decode()
        self.assertIn(">ANSWERED · B<", page)
        self.assertIn("answered, saved", page)
        listing = get(self.port, "/decisions").body.decode()
        self.assertIn('<div class="row pend saved">', listing)

    def test_a_write_in_is_saved_with_the_words_and_goes_back_to_the_list(self):
        resp = post(self.port, "/decisions/cache-ttl", "words=neither — ask the owner&back=/decisions".encode(),
                    host=f"localhost:{self.port}", origin=f"http://localhost:{self.port}")
        self.assertEqual((resp.status, resp.getheader("Location")), (303, "/decisions"))
        self.assertEqual((self.answer()["key"], self.answer()["words"]), (None, "neither — ask the owner"))
        self.assertIn("“neither — ask the owner”", get(self.port, "/decisions").body.decode())

    def test_what_is_not_the_users_answer_is_refused_and_nothing_is_saved(self):
        cases = (("/decisions/cache-ttl", b"key=B", "evil.test", self.origin, 403),
                 ("/decisions/cache-ttl", b"key=B", None, "http://evil.test", 403),
                 ("/decisions/cache-ttl", b"key=B", None, "null", 403),
                 ("/decisions/nope", b"key=B", None, self.origin, 404),
                 ("/decisions/../x", b"key=B", None, self.origin, 404),
                 ("/decisions/cache-ttl", b"key=Z", None, self.origin, 400),
                 ("/decisions/cache-ttl", b"words=+", None, self.origin, 400),
                 ("/decisions/cache-ttl", b"words=" + b"x" * 70000, None, self.origin, 413),
                 ("/decision-cache-ttl.html", b"key=B", None, self.origin, 404))
        for path, body, host, origin, status in cases:
            with self.subTest(path=path, body=body[:20], host=host, origin=origin):
                self.assertEqual(post(self.port, path, body, host=host, origin=origin).status, status)
        self.assertIsNone(self.answer())

    def test_an_unreadable_decision_takes_no_answer(self):
        self.json.write_text("{")
        self.assertEqual(post(self.port, "/decisions/cache-ttl", b"key=B", origin=self.origin).status, 409)


WAIT = 5.0   # the longest a test waits for something that must happen
QUIET = 0.5  # how long a test watches for something that must not happen


def decision(slug: str) -> str:
    return json.dumps({"headline": slug, "ask": "Which?", "options": [{"key": "A", "title": "Keep", "text": "no change"},
                       {"key": "B", "title": "Drop", "text": "slower"}], "recommended": "A", "why": "fine", "default": "A at 17:00"})


class _Renders:
    """Fake renderers in place of `render_board.write`, `decision_page.write` and `issue_page.write`: each records the page
    it renders on entry and holds until the test releases it, so a test sees how many renders are inside at once.

    The user, 2026-09-26: "allow the board server to have up to 4 workers", then "well, N workers. 4 default"."""

    SETTINGS = ""
    fake_refresh = RenderOnRequestTest.fake_refresh

    def setUp(self):
        RenderOnRequestTest.setUp(self)
        import decision_page
        import issue_page
        import render_board
        self.cond, self.gate = threading.Condition(), threading.Semaphore(0)
        self.inside, self.entered, self.most = [], [], {}
        for target, fake in ((render_board, lambda root, day=None: self.render(f"{day}-board.html")),
                             (decision_page, lambda root, pages_dir, slug, day, source=None: self.render(f"decision-{slug}.html")),
                             (issue_page, lambda root, pages_dir, n: self.render(f"issue-{n}.html"))):
            patcher = mock.patch.object(target, "write", fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.threads = []
        self.addCleanup(self.finish)

    def render(self, name: str):
        with self.cond:
            self.inside.append(name)
            self.entered.append(name)
            self.most[name] = max(self.most.get(name, 0), self.inside.count(name))
            self.most["*"] = max(self.most.get("*", 0), len(self.inside))
            self.cond.notify_all()
        try:
            self.gate.acquire(timeout=30)
            (self.pages / name).write_text(f"<!doctype html><body><p>{name}</p></body>")
        finally:
            with self.cond:
                self.inside.remove(name)
                self.cond.notify_all()

    def until(self, predicate, timeout: float) -> bool:
        with self.cond:
            return self.cond.wait_for(predicate, timeout)

    def fire(self, call, *args, **kwargs) -> dict:
        box = {}

        def run():
            try:
                box["resp"] = call(self.port, *args, **kwargs)
            except Exception as e:  # a client timeout: the test's own assertions report it
                box["error"] = e
        box["thread"] = threading.Thread(target=run, daemon=True)
        box["thread"].start()
        self.threads.append(box["thread"])
        return box

    def finish(self):
        self.gate.release(1000)
        for t in self.threads:
            t.join(WAIT)

    def assert_up_to(self, n: int):
        """Rule: "The server renders up to N pages at once." Rule: "The N+1th waits. It proceeds when one of the N finishes." """
        first = [self.fire(get, f"/issues/{i}") for i in range(1, n + 1)]
        self.assertTrue(self.until(lambda: len(self.inside) >= n, WAIT),
                        f"{n} different pages were requested; {len(self.inside)} rendered at once")
        last = self.fire(get, f"/issues/{n + 1}")
        self.assertFalse(self.until(lambda: len(self.entered) > n, QUIET), f"page {n + 1} rendered beside {n} others")
        self.gate.release()
        self.assertTrue(self.until(lambda: len(self.entered) > n, WAIT), f"page {n + 1} did not render when one of the {n} finished")
        self.gate.release(n)
        for box in [*first, last]:
            box["thread"].join(WAIT)
            self.assertEqual(box.get("resp") and box["resp"].status, 200, box.get("error"))
        self.assertEqual(self.most["*"], n)


class ConcurrentRenderTest(_Renders, unittest.TestCase):
    """A slow render (the module graph, the source & diff page) no longer holds up every other page."""

    def test_up_to_four_pages_render_at_once_by_default(self):
        # Rule: "N comes from the workspace settings TOML `[pages] workers` ... and defaults to 4." No [pages] here.
        self.assert_up_to(4)

    def test_one_page_renders_once_at_a_time_and_different_pages_overlap(self):
        # Rule: "Two requests for the same page never render it concurrently ... Different pages do overlap."
        a = self.fire(get, "/issues/1")
        self.assertTrue(self.until(lambda: "issue-1.html" in self.inside, WAIT))
        again, other = self.fire(get, "/issues/1"), self.fire(get, "/issues/2")
        self.assertTrue(self.until(lambda: "issue-2.html" in self.inside, WAIT),
                        "a render of another page waited for issue 1's render")
        self.assertFalse(self.until(lambda: self.inside.count("issue-1.html") > 1, QUIET), "issue 1 rendered twice at once")
        self.gate.release(10)
        for box in (a, again, other):
            box["thread"].join(WAIT)
            self.assertEqual(box.get("resp") and box["resp"].status, 200, box.get("error"))
        self.assertEqual(self.most["issue-1.html"], 1)

    def test_an_answer_waits_only_for_a_render_of_its_own_decision(self):
        # Rule: "a decision-answer save never overlaps a render of that same decision page. Different pages do overlap."
        for slug in ("cache-ttl", "log-level"):
            (self.pages / f"decision-{slug}.json").write_text(decision(slug))
        origin = f"http://127.0.0.1:{self.port}"
        page = self.fire(get, "/decisions/cache-ttl")
        self.assertTrue(self.until(lambda: "decision-cache-ttl.html" in self.inside, WAIT))
        same = self.fire(post, "/decisions/cache-ttl", b"key=B", origin=origin)
        other = self.fire(post, "/decisions/log-level", b"key=A", origin=origin)
        other["thread"].join(WAIT)
        self.assertEqual(other.get("resp") and other["resp"].status, 303,
                         f"the answer to another decision waited for this render: {other.get('error')}")
        self.assertEqual(json.loads((self.pages / "decision-log-level.json").read_text())["answer"]["key"], "A")
        same["thread"].join(QUIET)
        self.assertTrue(same["thread"].is_alive(), "the answer was saved while its decision page was rendering")
        self.assertNotIn("answer", json.loads((self.pages / "decision-cache-ttl.json").read_text()))
        self.gate.release(10)
        for box in (page, same):
            box["thread"].join(WAIT)
        self.assertEqual(same.get("resp") and same["resp"].status, 303, same.get("error"))
        self.assertEqual(json.loads((self.pages / "decision-cache-ttl.json").read_text())["answer"]["key"], "B")


class ConfiguredWorkersTest(_Renders, unittest.TestCase):
    SETTINGS = "[pages]\nworkers = 2\n"

    def test_pages_workers_sets_how_many_render_at_once(self):
        # Rule: "N comes from the workspace settings TOML `[pages] workers`, a positive whole number".
        self.assert_up_to(2)


if __name__ == "__main__":
    unittest.main()
