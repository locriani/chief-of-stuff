"""Escaping and validation shared by the HTML-fragment charts (gantt.py, columns.py) and the pages' links and tab bar.
It imports nothing from the plugin, so each chart lifts out with this file and no other."""

from __future__ import annotations

import html
import re

KIND = re.compile(r"[a-z][a-z0-9-]*")
COLOUR = re.compile(r"[#\w(),.%/ -]+")  # a colour, var() or border shorthand; nothing that ends a declaration or a rule


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def href(url: str) -> str:
    """`href="url"`, opening in a new tab when `url` is external (http/https); in-app routes stay in the tab."""
    blank = ' target="_blank" rel="noopener"' if url.lower().startswith(("http://", "https://")) else ""
    return f'href="{esc(url)}"{blank}'


def kind(name: str) -> str:
    """`name`, if it can stand as a CSS class."""
    if not KIND.fullmatch(name):
        raise ValueError(f"kind {name!r} is not a class name")
    return name


def css_str(text: str) -> str:
    """A CSS string body that cannot end the string, the rule or the `<style>` element."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("<", "\\3c ").replace("\n", "\\a ")


def colour(value: str) -> str:
    """`value`, if it cannot end its declaration or rule."""
    if not COLOUR.fullmatch(value):
        raise ValueError(f"colour {value!r} is not a colour")
    return value


# The Board sections nav (Flow.dc.html) atop every page: (name, route); a tab with no page yet has no route.
TABS = (("Board", "/"), ("Workers", "/workers"), ("Dispatch", ""), ("Since you last looked", ""), ("Epics", ""),
        ("Decisions", "/decisions"), ("Limits", ""))
# On the pages' tokens (--fg, --muted, --line, --link); wraps at phone width rather than widening the page.
# The pages' type: Alegreya Sans for text, Cormorant SC for heads.
FONTS = "https://fonts.googleapis.com/css2?family=Alegreya+Sans:wght@400;500;600&family=Cormorant+SC:wght@600&display=swap"
TAB_CSS = """nav.tabs{display:flex;flex-wrap:wrap;align-items:flex-end;column-gap:28px;margin:-8px 0 0;border-bottom:1px solid var(--line)}
nav.tabs>*{display:inline-flex;align-items:baseline;gap:7px;min-height:34px;box-sizing:border-box;padding:8px 0 7px;margin-bottom:-1px;font:600 15.5px "Cormorant SC",Georgia,serif;letter-spacing:.08em;text-decoration:none;color:var(--muted)}
nav.tabs>[aria-current=page]{color:var(--fg);box-shadow:inset 0 -3px 0 var(--fg)}
nav.tabs>.tab-parent{color:var(--fg);box-shadow:inset 0 -1px 0 var(--fg)}
nav.tabs .badge{font:700 11.5px ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:0;color:var(--link,var(--brass))}
"""


def tab_bar(tab: str, pending: int, parent: bool = False) -> str:
    """The nav, `tab` marked as this page's (`aria-current`), or as its parent's on a detail page (`parent`).
    Decisions carries `pending`, the count decisions.html heads PENDING with, when there is any."""
    mark = ' class="tab-parent"' if parent else ' aria-current="page"'
    out = []
    for name, url in TABS:
        badge = f'<span class="badge" title="{pending} pending">{pending}</span>' if name == "Decisions" and pending else ""
        attrs = mark if name == tab else ""
        out.append(f"<a {href(url)}{attrs}>{name}{badge}</a>" if url else f"<span{attrs}>{name}</span>")
    return '<nav class="tabs" aria-label="Board sections">' + "".join(out) + "</nav>\n"
