"""Escaping and validation shared by the HTML-fragment charts (gantt.py, columns.py).
It imports nothing from the plugin, so each chart lifts out with this file and no other."""

from __future__ import annotations

import html
import re

KIND = re.compile(r"[a-z][a-z0-9-]*")
COLOUR = re.compile(r"[#\w(),.%/ -]+")  # a colour, var() or border shorthand; nothing that ends a declaration or a rule


def esc(text: str) -> str:
    return html.escape(text, quote=True)


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
