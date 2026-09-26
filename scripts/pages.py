#!/usr/bin/env python3
"""Serve the workspace's pages locally: the board and any page a session writes beside it.

The pages are routes: `/` the board, `/decisions` the list, `/decisions/<slug>` a decision page. Each is
rendered into its file (`<day>-board.html`, `decisions.html`, `decision-<slug>.html`), and a GET re-renders it when
one of its sources (the trackers, CLAUDE.md, the settings TOML, the decision JSON, `.sources.json`) is
newer than the file, and a thread refreshes `.sources.json` from the forge every minute. An open tab
reloads itself when the file changes.

    python3 pages.py --ensure [--root R]              # start it unless it is already serving; print status only
    python3 pages.py serve --dir D --port P --root R  # what --ensure starts, detached

A decision page posts the user's answer to `POST /decisions/<slug>` (`key` and/or `words`, form-encoded).
The server saves it as the decision JSON's `answer` ({key, words, at}) for the coordinator's next pass, and
redirects back to the page, which then shows the decision answered.

The `## Coordinator` block's `Board:` line names both halves: `URL http://127.0.0.1:<port>/` and
``dir `<pages dir>` ``. It listens on 127.0.0.1 only, and it answers only requests whose Host is
127.0.0.1 or localhost. `/` is today's board once today's tracker exists, else the newest one; `/all` lists every page. Binding to loopback does not stop a page elsewhere from rebinding its own
name to 127.0.0.1, but that page still sends its own name as the Host.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import io
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from html import escape
from urllib.parse import parse_qs, quote, urlsplit

SERVER = "chief-of-stuff-pages"
PID = ".pid"
CACHE = ".sources.json"
REFRESH = 60  # seconds between forge reads
RENDERED = re.compile(r"\d{4}-\d{2}-\d{2}-board\.html|decisions\.html|decision-[a-z0-9]+(?:-[a-z0-9]+)*\.html")
ROUTE = re.compile(r"/decisions/([a-z0-9]+(?:-[a-z0-9]+)*)")
OLD = re.compile(r"/decision(?:s|-([a-z0-9]+(?:-[a-z0-9]+)*))\.html")  # the file URLs before the routes
MAX_BODY = 16 * 1024  # a write-in is a sentence or a paragraph
# ponytail: one lock for every render; per-page locks if renders ever get slow.
RENDER = threading.Lock()


def _plugin_version() -> str:
    try:
        return json.loads((Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json").read_text())["version"]
    except (OSError, ValueError, KeyError):
        return "0"


VERSION = _plugin_version()


def _version(server_header: str) -> tuple[int, ...]:
    """The version a Server header names; a server from before versions were sent is (0,)."""
    m = re.match(rf"{SERVER}/(\d+(?:\.\d+)*)", server_header)
    return tuple(int(x) for x in m[1].split(".")) if m else (0,)


class PagesError(RuntimeError):
    """The pages cannot be served: no Board line, or the port is someone else's."""


def _sources(root: Path, cfg, pages_dir: Path, day: str) -> list[Path]:
    from render_board import daily_trackers
    return [root / "CLAUDE.md", *([root / cfg.settings_path] if cfg.settings_path else []), root / cfg.log_path(day),
            *(path for _, path in daily_trackers(root, cfg)), *pages_dir.glob("decision-*.json"), pages_dir / CACHE]


def today_board(root: Path) -> str | None:
    """Today's board name once today's tracker exists, else None."""
    from render_board import parse_coordinator
    cfg = parse_coordinator((root / "CLAUDE.md").read_text(), today=date.today())
    day = datetime.now(cfg.zone).date().isoformat()
    return f"{day}-board.html" if (root / cfg.tracker_path(day)).is_file() else None


def fresh(root: Path, pages_dir: Path, name: str) -> str:
    """Re-render the page `name` when a source is newer than its file. Returns the render error, or "".
    On an error the last good file stays where it is."""
    import decision_page
    import render_board
    try:
        cfg = render_board.parse_coordinator((root / "CLAUDE.md").read_text(), today=date.today())
        today = datetime.now(cfg.zone).date().isoformat()
        if name.startswith("decision-"):
            if not (pages_dir / f"{name[:-5]}.json").is_file():
                return ""
            day = today
        elif name == "decisions.html":
            days = [d.isoformat() for d, _ in render_board.daily_trackers(root, cfg)]
            day = today if today in days or not days else days[-1]
        else:
            day = name[:10]
            if not (root / cfg.tracker_path(day)).is_file():
                return ""
        target = pages_dir / name
        newest = max((p.stat().st_mtime_ns for p in _sources(root, cfg, pages_dir, day) if p.is_file()), default=0)
        if target.is_file() and target.stat().st_mtime_ns >= newest:
            return ""
        with RENDER:
            if name.startswith("decision-"):
                decision_page.write(root, pages_dir, name[len("decision-"):-5], day)
            else:
                render_board.write(root, day)
        return ""
    except Exception as e:  # any renderer failure: the server keeps serving the last good page
        return f"{type(e).__name__}: {e}"


class Refresher(threading.Thread):
    """Calls `refresh(root, now)` every `every` seconds, and at once when `wake` is set."""

    def __init__(self, root: Path, refresh, every: float):
        super().__init__(daemon=True)
        self.root, self.refresh, self.every, self.wake = root, refresh, every, threading.Event()

    def run(self):
        while True:
            try:
                self.refresh(self.root, datetime.now().astimezone())
            except Exception as e:  # refresh never raises by contract; a bug in it must not end the thread
                print(f"pages: sources refresh failed: {e}", file=sys.stderr, flush=True)
            self.wake.wait(self.every)
            self.wake.clear()


class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = f"{SERVER}/{VERSION}"
    banner = ""

    def _allowed(self) -> bool:
        """Refuse a foreign Host or a dotfile; map `/` to today's (else the newest) board, `/all` to the listing and
        `/decisions[/<slug>]` to its file; redirect the old file URLs; re-render a page its sources outdate."""
        port = self.server.server_address[1]
        if self.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self.send_error(403, "Host is not this machine")
            return False
        path = self.path.split("?")[0]
        if any(part.startswith(".") for part in path.split("/")):
            self.send_error(404)
            return False
        root = getattr(self.server, "root", None)
        self._poke()
        if path == "/":
            # Served in place, not redirected: the tab stays on `/`, so its reload poll finds the next day's board.
            name = None
            if root is not None:
                try:
                    name = today_board(root)
                except Exception:  # an unreadable CLAUDE.md: fall back to the newest board on disk
                    pass
            boards = sorted(Path(self.directory).glob("*-board.html"))
            if name is None and not boards:
                self.send_error(404, "no board rendered yet")
                return False
            self.path = "/" + (name or boards[-1].name)
        elif path == "/all":
            self.path = "/"
        elif path == "/decisions":
            self.path = "/decisions.html"
        elif path.startswith("/decisions/"):
            m = ROUTE.fullmatch(path)
            if not m or not (Path(self.directory) / f"decision-{m[1]}.json").is_file():
                self.send_error(404, "no such decision")
                return False
            self.path = f"/decision-{m[1]}.html"
        elif old := OLD.fullmatch(path):
            self.send_response(301)
            self.send_header("Location", f"/decisions/{old[1]}" if old[1] else "/decisions")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        name = self.path.split("?")[0].lstrip("/")
        if root is not None and RENDERED.fullmatch(name):
            self.banner = fresh(root, Path(self.directory), name)
        return True

    def _poke(self):
        """Wake the refresher when the cache is older than its interval; the GET never waits for it."""
        refresher = getattr(self.server, "refresher", None)
        if refresher is None:
            return
        try:
            stale = time.time() - (Path(self.directory) / CACHE).stat().st_mtime > refresher.every
        except OSError:
            stale = True
        if stale:
            refresher.wake.set()

    def _send_with_banner(self, body: bool):
        """The last good page, with one line saying the render failed. Never a blank 500."""
        path = Path(self.translate_path(self.path))
        try:
            page, stamp = path.read_bytes(), path.stat().st_mtime
        except OSError:
            page, stamp = b"<!doctype html>\n<meta charset=\"utf-8\">\n<body>\n</body>\n", 0
        note = ('<p role="alert" style="margin:0;padding:6px 16px;background:#c9533a;color:#fff;'
                f'font:14px/1.4 system-ui,sans-serif">not re-rendered: {escape(self.banner)}</p>').encode()
        page = re.sub(rb"<body[^>]*>", lambda m: m[0] + note, page, count=1) if b"<body" in page else note + page
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Last-Modified", self.date_time_string(stamp))
        self.end_headers()
        if body:
            self.wfile.write(page)

    def do_GET(self):
        if self._allowed():
            self._send_with_banner(True) if self.banner else super().do_GET()

    def do_POST(self):
        """Save the user's answer beside the decision. Only this machine's own pages may post: the Host and, when a
        browser sends one, the Origin must be this server's."""
        port = self.server.server_address[1]
        own = (f"127.0.0.1:{port}", f"localhost:{port}")
        origin = self.headers.get("Origin")
        if self.headers.get("Host", "") not in own or (origin is not None and origin not in [f"http://{h}" for h in own]):
            return self.send_error(403, "not a page of this server")
        m = ROUTE.fullmatch(self.path.split("?")[0])
        source = Path(self.directory) / f"decision-{m[1]}.json" if m else None
        if source is None or not source.is_file():
            return self.send_error(404, "no such decision")
        try:
            size = int(self.headers["Content-Length"])
        except (TypeError, ValueError):
            return self.send_error(411)
        if size > MAX_BODY:
            self.close_connection = True
            return self.send_error(413, f"an answer is at most {MAX_BODY} bytes")
        form = parse_qs(self.rfile.read(size).decode("utf-8", "replace"))
        key, words = form.get("key", [""])[0], form.get("words", [""])[0].strip()
        import decision_page
        with RENDER:
            try:
                d = decision_page.parse(json.loads(source.read_text()))
            except decision_page.BAD as e:
                return self.send_error(409, f"the decision does not read: {e}")
            if (key and key not in [o["key"] for o in d["options"]]) or not (key or words):
                return self.send_error(400, "an answer is one of the options' keys, or words")
            d["answer"] = {"key": key or None, "words": words, "at": datetime.now().astimezone().isoformat(timespec="seconds")}
            stat = source.stat()
            tmp = source.with_name(f".{source.name}.tmp")
            tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
            os.replace(tmp, source)
            # The file's time is when it was asked (decision_page.Context.asked), so it keeps it; the pages it
            # outdates are marked stale instead, and the next GET renders them.
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            for page in Path(self.directory).iterdir():
                if RENDERED.fullmatch(page.name):
                    os.utime(page, (0, 0))
        self.send_response(303)
        self.send_header("Location", "/decisions" if form.get("back") == ["/decisions"] else f"/decisions/{m[1]}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        if self._allowed():
            self._send_with_banner(False) if self.banner else super().do_HEAD()

    def list_directory(self, path):
        # The stdlib listing, without dotfiles: the pid file is the server's, not a page.
        names = sorted(p.name for p in Path(path).iterdir() if not p.name.startswith("."))
        body = "".join(f'<li><a href="/{quote(n)}">{escape(n)}</a></li>' for n in names).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        return io.BytesIO(body)

    def guess_type(self, path):
        # Every page here is written as UTF-8; without a charset a browser falls back to Windows-1252.
        kind = super().guess_type(path)
        return f"{kind}; charset=utf-8" if kind.startswith("text/") else kind

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, *_args):
        pass


def make_server(pages_dir: Path, port: int, root: Path | None = None, refresh=None,
                every: float = REFRESH) -> http.server.ThreadingHTTPServer:
    """With `root`, pages render on request and a Refresher keeps `.sources.json` current."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                             functools.partial(Handler, directory=str(pages_dir)))
    server.root, server.refresher = root, None
    if root is not None:
        if refresh is None:
            from board_sources import refresh
        server.refresher = Refresher(root, refresh, every)
        server.refresher.start()
    return server


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


def _stop(pages_dir: Path, port: int) -> None:
    """Stop the older pages server on the port. Only the pid file's process, and only when it runs
    `pages.py serve` on this port: a reused pid is someone else's."""
    try:
        pid = int((pages_dir / PID).read_text())
    except (OSError, ValueError):
        raise PagesError(f"an older pages server answers on port {port}, and no pid file names it; stop it by hand") from None
    command = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    if "pages.py serve" not in command or not re.search(rf"--port {port}(\s|$)", command):
        raise PagesError(f"an older pages server answers on port {port}, but pid {pid} is not it; stop it by hand")
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if _who(port) is None:
            return
        time.sleep(0.1)
    raise PagesError(f"the older pages server (pid {pid}) is still answering on port {port}")


def ensure(pages_dir: Path, port: int, log: Path, root: Path | None = None) -> str:
    """Start the server unless ours already answers on the port; replace one older than this code. Never
    moves to another port: the URL is the board's address, and a board that wanders is a board nobody has open."""
    who = _who(port)
    restart = False
    if who is not None:
        if not who.startswith(SERVER):
            raise PagesError(f"port {port} answers as {who or 'an unnamed server'}, not the pages server")
        if _version(who) >= _version(f"{SERVER}/{VERSION}"):
            return "pages: serving"
        _stop(pages_dir, port)
        restart = True
    pages_dir.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, str(Path(__file__).resolve()), "serve", "--dir", str(pages_dir), "--port", str(port)]
    with log.open("a") as out:
        proc = subprocess.Popen(argv + (["--root", str(root.resolve())] if root is not None else []),
                                stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)
    (pages_dir / PID).write_text(str(proc.pid))
    for _ in range(30):
        if (_who(port) or "").startswith(SERVER):
            return "pages: restarted" if restart else "pages: started"
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
    ap.add_argument("--ensure", action="store_true", help="start the server unless it is serving; print status only")
    ap.add_argument("--root", help="workspace root holding CLAUDE.md (default: the current directory)")
    ap.add_argument("command", nargs="?", choices=("serve",))
    ap.add_argument("--dir")
    ap.add_argument("--port", type=int)
    args = ap.parse_args(argv)
    if args.command == "serve":
        make_server(Path(args.dir), args.port, Path(args.root) if args.root else None).serve_forever()
        return 0
    root = Path(args.root or ".")
    try:
        pages_dir, port = from_config(root)
        print(ensure(pages_dir, port, root / ".chief-of-stuff" / "pages.log", root))
    except PagesError as e:
        print(f"pages: not served — {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
