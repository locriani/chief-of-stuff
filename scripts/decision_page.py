#!/usr/bin/env python3
"""Render one decision's full context as a page in the pages dir, and print its local URL.

    python3 decision_page.py --root R --name <slug> [--from decision.json] [--date YYYY-MM-DD]

The decision is read from `<pages dir>/decision-<slug>.json` unless `--from` names another file: a
file, because Claude Code refuses JSON piped through a heredoc. The page is `<pages dir>/decision-<slug>.html`, served by pages.py at `http://127.0.0.1:<port>/decision-<slug>.html`.
Never a claude.ai artifact (Zach, 2026-09-25: "don't serve it as an artifact but serve it using the localhost
server thing"). The JSON holds:

    headline, ask, options [{key, title, text}], recommended (a key), why, default   required
    topic, yes, sections [{heading, text: paragraph or [paragraph]}], sides [{label, status: open|done, text}],
    timeline [{when, text}], references [{label, value}], sources                    optional

Text is escaped; `code`, **bold** and http(s) URLs are marked up.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from html import escape
from pathlib import Path

from pages import PagesError, from_config

NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
REQUIRED = ("headline", "ask", "options", "recommended", "why", "default")


class DecisionError(ValueError):
    """The JSON does not describe a decision the user can make from the page."""


def parse(data: dict) -> dict:
    """The decision itself, once it holds everything the user decides from."""
    for key in REQUIRED:
        if not data.get(key):
            raise DecisionError(f"the decision has no {key}")
    keys = [o.get("key") for o in data["options"]]
    if len(keys) < 2 or len(set(keys)) != len(keys) or not all(keys):
        raise DecisionError("a decision needs at least two options, each with its own key")
    if data["recommended"] not in keys:
        raise DecisionError(f"recommended {data['recommended']!r} is not one of the options {', '.join(keys)}")
    for side in data.get("sides", []):
        if side.get("status") not in ("open", "done"):
            raise DecisionError(f"side {side.get('label')!r} has status {side.get('status')!r}, not open or done")
    return data


def _md(text: str) -> str:
    """Escape, then mark up `code`, **bold** and URLs."""
    out = escape(str(text), quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", out)
    return re.sub(r"(?<![\w/>])(https?://[^\s<`]*[^\s<`.,;:)])", r'<a href="\1">\1</a>', out)


def _section(heading: str, body: str) -> str:
    return f"  <section>\n    <h2>{escape(heading, quote=False)}</h2>\n{body}\n  </section>\n"


def render(d: dict, day: str) -> str:
    parts = []
    for s in d.get("sections", []):
        paras = [s["text"]] if isinstance(s.get("text"), str) else s.get("text", [])
        parts.append(_section(s["heading"], "\n".join(f"    <p>{_md(p)}</p>" for p in paras)))
    if d.get("sides"):
        sides = "\n".join(f'      <div class="side">\n        <span class="pill {s["status"]}">{escape(s["label"])}</span>\n'
                          f'        <p>{_md(s["text"])}</p>\n      </div>' for s in d["sides"])
        parts.append(_section("Where it stands", f'    <div class="compare">\n{sides}\n    </div>'))
    if d.get("timeline"):
        items = "\n".join(f'      <li><div class="when">{escape(t["when"])}</div>{_md(t["text"])}</li>' for t in d["timeline"])
        parts.append(_section("How it got here", f'    <ol class="timeline">\n{items}\n    </ol>'))
    opts = []
    for o in d["options"]:
        rec = o["key"] == d["recommended"]
        title = escape(o["title"]) + (" (recommended)" if rec else "")
        opts.append(f'      <div class="opt{" rec" if rec else ""}"><span class="k">{escape(o["key"])}</span><b>{title}</b>'
                    f'\n        <p>{_md(o.get("text", ""))}</p>\n      </div>')
    parts.append(_section("Options", '    <div class="options">\n' + "\n".join(opts) + "\n    </div>"))
    parts.append(_section("Recommendation", f"    <p>{_md(d['why'])}</p>"))
    parts.append(_section("If you don't answer", f"    <p>{_md(d['default'])}</p>"))
    if d.get("references"):
        rows = "\n".join(f"        <tr><th>{escape(r['label'])}</th><td>{_md(r['value'])}</td></tr>" for r in d["references"])
        parts.append(_section("References", f'    <div class="tablewrap">\n      <table class="facts">\n{rows}\n      </table>\n    </div>'))
    keys = [o["key"] for o in d["options"]]
    answer = keys[0] if len(keys) == 1 else ", ".join(keys[:-1]) + (", or " if len(keys) > 2 else " or ") + keys[-1]
    sources = f" from {_md(d['sources'])}" if d.get("sources") else ""
    eyebrow = " · ".join(escape(x) for x in ("Decision", d.get("topic"), day) if x)
    yes = f"\n    <p>{_md(d['yes'])}</p>" if d.get("yes") else ""
    return (f"<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>{escape(d['headline'])}</title>\n{HEAD}</head>\n<body>\n<main>\n"
            f'  <header style="display:grid;gap:10px">\n    <span class="eyebrow">{eyebrow}</span>\n'
            f"    <h1>{_md(d['headline'])}</h1>\n  </header>\n\n"
            f'  <div class="ask">\n    <span class="eyebrow">What I need from you</span>\n'
            f"    <strong>{_md(d['ask'])}</strong>{yes}\n  </div>\n\n"
            + "\n".join(parts)
            + f'\n  <p class="foot">Prepared by the coordinator{sources}. Answer in chat: {answer}.</p>\n</main>\n</body>\n</html>\n')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=".", help="workspace root holding CLAUDE.md")
    ap.add_argument("--from", dest="source", help="the decision as JSON; default <pages dir>/decision-<slug>.json")
    ap.add_argument("--name", required=True, help="the page's slug: lowercase letters, digits and dashes")
    ap.add_argument("--date", default=date.today().isoformat(), help="the day shown on the page")
    args = ap.parse_args(argv)
    if not NAME.fullmatch(args.name):
        print(f"decision: name {args.name!r} is not lowercase letters, digits and dashes", file=sys.stderr)
        return 2
    try:
        pages_dir, port = from_config(Path(args.root))
    except PagesError as e:
        print(f"decision: no page — {e}", file=sys.stderr)
        return 1
    try:
        source = Path(args.source) if args.source else pages_dir / f"decision-{args.name}.json"
        page = render(parse(json.loads(source.read_text())), args.date)
    except (OSError, json.JSONDecodeError, DecisionError, KeyError, TypeError, AttributeError) as e:
        print(f"decision: {e}", file=sys.stderr)
        return 2
    pages_dir.mkdir(parents=True, exist_ok=True)
    (pages_dir / f"decision-{args.name}.html").write_text(page)
    print(f"http://127.0.0.1:{port}/decision-{args.name}.html")
    return 0


HEAD = """<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Serif:wght@500;600&display=swap">
<style>
:root{
  --ground:#F5F6F3; --panel:#FFFFFF; --ink:#1C2226; --muted:#5A646B; --rule:#D8DCD5;
  --accent:#2F6B5E; --accent-soft:#E3EFEB; --open:#9A5A0A; --open-soft:#F6EBD9; --done:#2D7449; --done-soft:#E1F0E6;
  --sans:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --serif:"IBM Plex Serif",Georgia,"Times New Roman",serif;
  --mono:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    color-scheme:dark;
    --ground:#14191B; --panel:#1B2225; --ink:#E6EAE7; --muted:#9AA5A6; --rule:#2E3739;
    --accent:#7DC0AE; --accent-soft:#1F332E; --open:#E3A857; --open-soft:#3A2C17; --done:#7FCB97; --done-soft:#1C3325;
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --ground:#14191B; --panel:#1B2225; --ink:#E6EAE7; --muted:#9AA5A6; --rule:#2E3739;
  --accent:#7DC0AE; --accent-soft:#1F332E; --open:#E3A857; --open-soft:#3A2C17; --done:#7FCB97; --done-soft:#1C3325;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font:15px/1.6 var(--sans);padding-inline:16px;padding-block:32px 56px}
main{max-width:760px;margin:0 auto;display:grid;gap:36px}
h1,h2{font-family:var(--serif);font-weight:600;text-wrap:balance;margin:0}
h1{font-size:30px;line-height:1.2}
h2{font-size:19px;margin-bottom:10px}
p{margin:0;max-width:65ch}
a{color:var(--accent);overflow-wrap:anywhere}
section{display:grid;gap:12px}
.eyebrow{font:500 12px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.ask{background:var(--accent-soft);border:1px solid var(--accent);border-radius:6px;padding:18px 20px;display:grid;gap:8px}
.ask strong{font-family:var(--serif);font-size:20px;font-weight:600}
code,.mono{font-family:var(--mono);font-size:13px}
.compare{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.side{background:var(--panel);border:1px solid var(--rule);border-radius:6px;padding:14px 16px;display:grid;gap:6px;align-content:start}
.pill{justify-self:start;font:500 11px/1 var(--mono);letter-spacing:.06em;text-transform:uppercase;padding:5px 8px;border-radius:3px}
.pill.open{color:var(--open);background:var(--open-soft)}
.pill.done{color:var(--done);background:var(--done-soft)}
.side p{font-size:14px}
.timeline{list-style:none;margin:0;padding:0;display:grid;gap:0;border-left:2px solid var(--rule)}
.timeline li{padding:0 0 14px 18px;position:relative}
.timeline li::before{content:"";position:absolute;left:-6px;top:7px;width:10px;height:10px;border-radius:50%;background:var(--panel);border:2px solid var(--accent)}
.timeline .when{font:500 12px/1.4 var(--mono);color:var(--muted);font-variant-numeric:tabular-nums}
.options{display:grid;gap:10px}
.opt{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;background:var(--panel);border:1px solid var(--rule);border-radius:6px;padding:14px 16px}
.opt.rec{border-color:var(--accent);box-shadow:inset 3px 0 0 var(--accent)}
.opt .k{font:600 13px/1.5 var(--mono);color:var(--accent);grid-row:span 2}
.opt b{font-weight:600}
.opt p{font-size:14px;color:var(--muted)}
.facts{width:100%;border-collapse:collapse;font-size:14px}
.facts th,.facts td{text-align:left;vertical-align:top;padding:8px 10px;border-top:1px solid var(--rule)}
.facts th{font-weight:500;color:var(--muted);white-space:nowrap;width:1%}
.tablewrap{overflow-x:auto}
.foot{font-size:13px;color:var(--muted);border-top:1px solid var(--rule);padding-top:14px}
</style>
"""


if __name__ == "__main__":
    raise SystemExit(main())
