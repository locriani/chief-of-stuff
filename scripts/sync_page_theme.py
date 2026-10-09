#!/usr/bin/env python3
"""Keep assets/page-theme.css a verbatim copy of the canonical page theme.

The canonical file is frank-lloyd-aight's `docs/page-theme.css`; the pinned source is its raw URL at
commit a981dfb (stage 1 of the page-theme plan, approved 2026-10-07). The script fetches it, holds the
bytes to the recorded sha256, and copies them into assets/ only when they differ — a drift in the
source, a failed fetch, or a copy that does not read back exits non-zero and changes nothing. Standard
library only; the standalone install copies this file beside the renderers that inline the asset.

    python3 scripts/sync_page_theme.py                # fetch the pinned source, verify, copy
    python3 scripts/sync_page_theme.py --check        # verify the vendored copy only; fetch nothing
    python3 scripts/sync_page_theme.py --source URL   # verify against another source of the same bytes
"""

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

SOURCE = "https://raw.githubusercontent.com/locriani/frank-lloyd-aight/a981dfb/docs/page-theme.css"
PINNED_SHA256 = "3bb3d66878142d5ae981529894a1572ea8e6c3ccf5ac5b7d52ba1d591aaf8b3c"
ASSET = Path(__file__).resolve().parent.parent / "assets" / "page-theme.css"


def _fetch(source: str) -> bytes:
    resp = urllib.request.urlopen(source, timeout=30)
    try:
        return resp.read()
    finally:
        resp.close()


def _die(message: str) -> int:
    print(f"sync_page_theme: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", default=SOURCE, help="the canonical file's URL (default: the pinned commit)")
    ap.add_argument("--check", action="store_true", help="verify the vendored copy's sha256 only; fetch nothing")
    args = ap.parse_args(argv)
    if args.check:
        vendored = hashlib.sha256(ASSET.read_bytes()).hexdigest() if ASSET.is_file() else None
        if vendored == PINNED_SHA256:
            return 0
        return _die("the vendored copy drifted from the recorded sha256 "
                    f"{PINNED_SHA256}" + (f": {vendored}" if vendored else ": it is missing"))
    try:
        canonical = _fetch(args.source)
    except OSError as e:
        return _die(f"cannot fetch {args.source}: {e}")
    got = hashlib.sha256(canonical).hexdigest()
    if got != PINNED_SHA256:
        return _die(f"{args.source} is not the pinned theme: sha256 {got}, recorded {PINNED_SHA256}")
    if ASSET.is_file() and ASSET.read_bytes() == canonical:
        print(f"sync_page_theme: {ASSET} already matches {PINNED_SHA256[:12]}")
        return 0
    ASSET.parent.mkdir(parents=True, exist_ok=True)
    ASSET.write_bytes(canonical)
    if hashlib.sha256(ASSET.read_bytes()).hexdigest() != PINNED_SHA256:  # read back: the copy is the proof
        return _die(f"wrote {ASSET} but it does not read back as {PINNED_SHA256}")
    print(f"sync_page_theme: copied the pinned theme ({len(canonical)} bytes, sha256 {PINNED_SHA256[:12]}) to {ASSET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
