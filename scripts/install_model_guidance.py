#!/usr/bin/env python3
"""Copy the editable worker model guidance template into a workspace once."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "assets" / "worker-model-guidance.md"
NAME = "chief-of-stuff-models.md"


def destination(root: Path) -> Path:
    if not root.is_dir():
        raise ValueError(f"workspace {root} is not a directory")
    path = root / NAME
    if path.exists() and not path.is_file():
        raise ValueError(f"{path} exists but is not a file")
    return path


def install(root: Path, *, dry_run: bool = False) -> tuple[Path, bool]:
    """Return (path, created); preserve every existing workspace edit."""
    path = destination(root)
    if path.exists():
        return path, False
    if not dry_run:
        body = TEMPLATE.read_text()
        with path.open("x") as output:
            output.write(body)
    return path, True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        path, created = install(args.root.resolve(), dry_run=args.dry_run)
    except (OSError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"{'would create' if args.dry_run and created else 'created' if created else 'kept existing'}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
