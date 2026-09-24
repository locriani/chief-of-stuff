#!/usr/bin/env python3
"""Serve the workspace's pages from this Mac: the board and any page a session writes beside it.

Zach, 2026-09-24 14:07: "we should host our own webserver and ensure they are set up as part of the
agent's boot loop." It replaced the claude.ai Artifact board. The page is the file the renderer
wrote, so a move renders and is done, and the open tab reloads itself when the file changes.

    python3 pages.py --ensure [--root R]    # start it unless it is already serving; print its URL
    python3 pages.py serve --dir D --port P # what --ensure starts, detached

The `## Coordinator` block's `Board:` line names both halves: `URL http://127.0.0.1:<port>/` and
``dir `<pages dir>` ``. It listens on 127.0.0.1 only, and it answers only requests whose Host is
127.0.0.1 or localhost. `/` is the newest board, `/all` lists every page. Binding to loopback does not stop a page elsewhere from rebinding its own
name to 127.0.0.1, but that page still sends its own name as the Host.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import io
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from html import escape
from urllib.parse import quote, urlsplit

SERVER = "chief-of-stuff-pages"
PID = ".pid"


class PagesError(RuntimeError):
    """The pages cannot be served: no Board line, or the port is someone else's."""


class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = SERVER

    def _allowed(self) -> bool:
        """Refuse a foreign Host or a dotfile; map `/` to the newest board and `/all` to the listing."""
        port = self.server.server_address[1]
        if self.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self.send_error(403, "Host is not this machine")
            return False
        path = self.path.split("?")[0]
        if any(part.startswith(".") for part in path.split("/")):
            self.send_error(404)
            return False
        if path == "/":
            # Served in place, not redirected: the tab stays on `/`, so its reload poll finds the next day's board.
            boards = sorted(Path(self.directory).glob("*-board.html"))
            if not boards:
                self.send_error(404, "no board rendered yet")
                return False
            self.path = "/" + boards[-1].name
        elif path == "/all":
            self.path = "/"
        return True

    def do_GET(self):
        if self._allowed():
            super().do_GET()

    def do_HEAD(self):
        if self._allowed():
            super().do_HEAD()

    def list_directory(self, path):
        # The stdlib listing, without dotfiles: the pid file is the server's, not a page.
        names = sorted(p.name for p in Path(path).iterdir() if not p.name.startswith("."))
        body = "".join(f'<li><a href="/{quote(n)}">{escape(n)}</a></li>' for n in names).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        return io.BytesIO(body)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, *_args):
        pass


def make_server(pages_dir: Path, port: int) -> http.server.ThreadingHTTPServer:
    return http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                           functools.partial(Handler, directory=str(pages_dir)))


def _who(port: int) -> str | None:
    """The Server header on the port, "" for a server that sends none, None when nothing answers."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/all", method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=1) as resp:
            return resp.headers.get("Server", "")
    except urllib.error.HTTPError as e:
        return e.headers.get("Server", "")
    except OSError:
        return None


def ensure(pages_dir: Path, port: int, log: Path) -> str:
    """Start the server unless ours already answers on the port. Never moves to another port: the URL
    is the board's address, and a board that wanders is a board nobody has open."""
    url = f"http://127.0.0.1:{port}/"
    who = _who(port)
    if who is not None:
        if who.startswith(SERVER):
            return f"pages: {url}"
        raise PagesError(f"port {port} answers as {who or 'an unnamed server'}, not the pages server")
    pages_dir.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    # ponytail: a running server keeps the code it started with across a plugin update; restart it by
    # pid when the server itself starts changing.
    with log.open("a") as out:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve", "--dir", str(pages_dir),
                                 "--port", str(port)], stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                                start_new_session=True)
    (pages_dir / PID).write_text(str(proc.pid))
    for _ in range(30):
        if (_who(port) or "").startswith(SERVER):
            return f"pages: started {url}"
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    raise PagesError(f"the pages server did not come up on port {port}; see {log}")


def from_config(root: Path) -> tuple[Path, int]:
    """(pages dir, port) from the block's Board line."""
    from render_board import ConfigError, parse_coordinator
    try:
        cfg = parse_coordinator((root / "CLAUDE.md").read_text(), today=date.today())
    except (OSError, ConfigError) as e:
        raise PagesError(str(e)) from None
    port = urlsplit(cfg.board_url or "").port
    if not cfg.pages_dir or not port:
        raise PagesError("the Board: line names no `URL http://127.0.0.1:<port>/` and `dir` to serve")
    return root / cfg.pages_dir, port


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ensure", action="store_true", help="start the server unless it is serving; print its URL")
    ap.add_argument("--root", default=".", help="workspace root holding CLAUDE.md")
    ap.add_argument("command", nargs="?", choices=("serve",))
    ap.add_argument("--dir")
    ap.add_argument("--port", type=int)
    args = ap.parse_args(argv)
    if args.command == "serve":
        make_server(Path(args.dir), args.port).serve_forever()
        return 0
    root = Path(args.root)
    try:
        pages_dir, port = from_config(root)
        print(ensure(pages_dir, port, root / ".chief-of-stuff" / "pages.log"))
    except PagesError as e:
        print(f"pages: not served — {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
