"""pages.py: the workspace's pages, served from this Mac (Zach, 2026-09-24 14:07: "we should host our own
webserver and ensure they are set up as part of the agent's boot loop")."""

import http.client
import http.server
import os
import signal
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

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

    def test_all_lists_every_page(self):
        resp = get(self.port, "/all")
        self.assertEqual(resp.status, 200)
        for name in (b"arch-review.html", b"2026-09-23-board.html"):
            self.assertIn(name, resp.body)
        self.assertNotIn(b".pid", resp.body)

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
        self.assertEqual(first, f"pages: started http://127.0.0.1:{self.port}/")
        pid = (self.dir / ".pid").read_text()
        self.assertEqual(get(self.port, "/").status, 200)
        self.assertEqual(pg.ensure(self.dir, self.port, self.log), f"pages: http://127.0.0.1:{self.port}/")
        self.assertEqual((self.dir / ".pid").read_text(), pid)

    def test_a_foreign_server_on_the_port_is_an_error(self):
        other = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        with self.assertRaisesRegex(pg.PagesError, f"{self.port}.*not the pages server"):
            pg.ensure(self.dir, self.port, self.log)


if __name__ == "__main__":
    unittest.main()
