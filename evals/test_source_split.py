"""source_page.py, slice 2: the split view of the "Source & diff" page (Source48.dc.html, the "Diff layout" control).
The unified view's contract (test_source_page's docstring) is unchanged. Out of scope: coverage gutter, review notes,
compare modes, tags.

Markup contract these tests read (keep to it; everything else is free). The page is a static file, so the control works
without JavaScript:

- The layout control is one element carrying `aria-label="Diff layout"`. It holds two radio inputs,
  `<input type="radio" name="layout" id="layout-unified">` and `<input type="radio" name="layout" id="layout-split">`,
  each named by a `<label for="<its id>">` whose text is `Unified` / `Split`. The Unified input is `checked`; the Split
  input is not.
- Each file's split diff is one `<table class="split" data-split-path="<the file's path>">` (no `data-path`, which marks
  the unified table). A rule in `<style id="source">` whose selector names `#layout-split:checked` also names the split
  tables (`.split` or `[data-split-path]`): the CSS shows them only when Split is chosen.
- Each split line row is one `<tr>` holding the cells `<td class="lno">` (old line number), `<td class="left KIND">`
  (old side's text), `<td class="rno">` (new line number) and `<td class="right KIND">` (new side's text). KIND is `ctx`,
  `del` (left only), `add` (right only) or `empty` (an unpaired side: its text and number cells are empty). Text cells
  hold only the line's text, escaped; a sign, if shown, sits in a cell of its own. Rows without `left`/`right` cells
  (hunk headers) are free.
- Inline `style` attributes stay free of hex colours (test_source_page's colour rule).
"""

import re
import sys
import unittest
from html import unescape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_source_page import LEGACY, LIMITS, MARKUP, QUOTA, SourcePage, tables, text  # noqa: E402


def control(html: str) -> str:
    found = re.findall(r'<(\w+)\b[^>]*\baria-label="Diff layout"[^>]*>(.*?)</\1>', html, re.S)
    if len(found) != 1:
        raise AssertionError(f'expected one element with aria-label="Diff layout", found {len(found)}')
    return found[0][1]


def radios(block: str) -> dict[str, str]:
    """The control's radio inputs by id: their opening tags."""
    out = {}
    for tag in re.findall(r"<input\b[^>]*>", block):
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', tag))
        if attrs.get("type") == "radio" and attrs.get("name") == "layout":
            out[attrs.get("id", "")] = tag
    return out


def splits(html: str) -> dict[str, str]:
    """Each file's split table by path. Contract: `<table class="split" data-split-path=…>`."""
    out = {}
    for m in re.finditer(r"<table\b([^>]*)>.*?</table>", html, re.S):
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', m[1]))
        if "data-split-path" in attrs and "split" in attrs.get("class", "").split():
            out[unescape(attrs["data-split-path"])] = m[0]
    return out


def pairs(table: str) -> list[dict]:
    """Each split line row: left/right kind and text, lno/rno, and the raw row."""
    out = []
    for body in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table, re.S):
        cells = {}
        for cls, inner in re.findall(r'<td\b[^>]*class="([^"]*)"[^>]*>(.*?)</td>', body, re.S):
            words = cls.split()
            for side in ("left", "right", "lno", "rno"):
                if side in words:
                    kind = next((k for k in ("ctx", "del", "add", "empty") if k in words), None)
                    cells[side] = (kind, unescape(re.sub(r"<[^>]+>", "", inner)))
        if "left" in cells or "right" in cells:
            left, right = cells.get("left", (None, None)), cells.get("right", (None, None))
            out.append({"l": left[0], "lt": left[1], "r": right[0], "rt": right[1], "raw": body,
                        "lno": (cells.get("lno", (None, ""))[1] or "").strip(),
                        "rno": (cells.get("rno", (None, ""))[1] or "").strip()})
    return out


class SplitTest(SourcePage):
    def split(self, path: str) -> list[dict]:
        got = splits(self.page())
        self.assertIn(path, got, f"no split table for {path}")
        return pairs(got[path])

    def test_one_layout_control_opens_on_unified(self):
        # "The diff layout is one control with Unified and Split; the page opens on Unified."
        html = self.page()
        block = control(html)
        got = radios(block)
        self.assertEqual(set(got), {"layout-unified", "layout-split"}, block)
        for rid, word in (("layout-unified", "Unified"), ("layout-split", "Split")):
            with self.subTest(radio=rid):
                label = re.search(rf'<label\b[^>]*\bfor="{rid}"[^>]*>(.*?)</label>', block, re.S)
                self.assertIsNotNone(label, f"no <label for={rid}> in the control")
                self.assertEqual(text(label[1]), word)
        self.assertRegex(got["layout-unified"], r"\bchecked\b")
        self.assertNotRegex(got["layout-split"], r"\bchecked\b")
        own = re.search(r'<style\b[^>]*\bid="source"[^>]*>(.*?)</style>', html, re.S)
        self.assertIsNotNone(own, 'no <style id="source">')
        selectors = re.findall(r"([^{}]+)\{", own[1])
        self.assertTrue(any("#layout-split:checked" in s and re.search(r"\.split\b|\[data-split-path\]", s)
                            for s in selectors), "no CSS rule ties the Split control to the split tables")

    def test_old_side_left_new_side_right(self):
        # "Split puts the old side left and the new side right: a context line on both sides with its old and new
        # numbers, a removed line on the left only, an added line on the right only."
        ctx = next((r for r in self.split(LIMITS) if r["lt"] == "def allowed(size):"), None)
        self.assertIsNotNone(ctx, "no split row for the context line")
        self.assertEqual((ctx["l"], ctx["lno"], ctx["r"], ctx["rt"], ctx["rno"]),
                         ("ctx", "4", "ctx", "def allowed(size):", "4"))
        for path in (LIMITS, QUOTA, LEGACY):
            with self.subTest(path=path):
                got = self.split(path)
                self.assertTrue(got)
                self.assertTrue({r["l"] for r in got} <= {"ctx", "del", "empty"}, got)
                self.assertTrue({r["r"] for r in got} <= {"ctx", "add", "empty"}, got)
                adds, dels = self.numstat[path]
                self.assertEqual(sum(r["r"] == "add" for r in got), adds)
                self.assertEqual(sum(r["l"] == "del" for r in got), dels)
        for r in self.split(LEGACY):  # removed file: every line on the left, numbered, the right empty
            self.assertEqual((r["l"], r["r"], r["rt"], r["rno"]), ("del", "empty", "", ""), r)
            self.assertTrue(r["lno"].isdigit(), r)
        for r in self.split(QUOTA):  # new file: every line on the right, numbered, the left empty
            self.assertEqual((r["l"], r["lt"], r["lno"], r["r"]), ("empty", "", "", "add"), r)
            self.assertTrue(r["rno"].isdigit(), r)

    def test_change_lines_pair_row_by_row(self):
        # "Within a change, removed and added lines are paired row by row; the shorter side is left empty."
        got = self.split(LIMITS)
        swap = next((r for r in got if r["lt"] == "MAX_MB = 10"), None)
        self.assertIsNotNone(swap, "no split row for the removed line")
        self.assertEqual((swap["l"], swap["lno"], swap["r"], swap["rt"], swap["rno"]),
                         ("del", "1", "add", "MAX_MB = 25", "1"))
        for tx, no in ((MARKUP, "6"), ("LIMIT_NAME = 'upload'", "7")):  # two adds, no dels: the left is empty
            with self.subTest(line=tx):
                row = next((r for r in got if r["rt"] == tx), None)
                self.assertIsNotNone(row, f"no split row whose right text is {tx!r}")
                self.assertEqual((row["l"], row["lt"], row["lno"], row["r"], row["rno"]), ("empty", "", "", "add", no))
        self.assertFalse([r for r in got if r["l"] == r["r"] == "empty"], "a row with both sides empty")

    def test_every_diff_has_a_split_and_its_text_is_text(self):
        # "Every file with a unified diff has a split diff, and split text is shown as text, never as markup."
        html = self.page()
        self.assertTrue(tables(html))
        self.assertEqual(set(splits(html)), set(tables(html)))
        self.assertNotIn("<script>alert(1)", html)
        row = next((r for r in self.split(LIMITS) if r["rt"] == MARKUP), None)
        self.assertIsNotNone(row, "the added line holding markup is not a split row whose right text is that line")
        self.assertIn("&lt;script&gt;", row["raw"])


if __name__ == "__main__":
    unittest.main()
