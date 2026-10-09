"""#577: the vendored TOON string escapes round-trip, `\\uXXXX` included, and the write format does not move.

The vendored `toon_format` is patched in place (no upgrade): `unescape_string` decodes `\\uXXXX` — four hex digits in
either case, a surrogate pair as one character, a lone surrogate replaced, as the launcher's own `utf-8, errors="replace"`
report writes replace one — and `escape_string` still emits exactly what it emitted before the patch: non-ASCII stays
literal, so TOON output for previously-encodable content is byte-identical.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from _vendor.toon_format import encode as toon_encode  # noqa: E402
from _vendor.toon_format import decode as toon_decode  # noqa: E402
from _vendor.toon_format._string_utils import escape_string, unescape_string  # noqa: E402


class UnescapeTests(unittest.TestCase):
    def test_a_u_escape_decodes_four_hex_digits_in_either_case(self):
        for escaped, char in (("\\u2014", "\u2014"), ("\\u00e9", "\u00e9"), ("\\u00C9", "\u00C9"), ("\\uffff", "\uffff")):
            with self.subTest(escaped):
                self.assertEqual(unescape_string(escaped), char)

    def test_a_surrogate_pair_escape_decodes_to_one_character(self):
        self.assertEqual(unescape_string("\\ud83d\\ude00"), "\U0001F600")
        self.assertEqual(unescape_string("x \\ud83d\\ude00 y"), "x \U0001F600 y")

    def test_a_lone_surrogate_escape_reads_as_the_replacement_character(self):
        self.assertEqual(unescape_string("x \\ud800 end"), "x \ufffd end")
        self.assertEqual(unescape_string("\\ude00"), "\ufffd")

    def test_an_invalid_u_escape_is_still_invalid(self):
        for escaped in ("\\u12\\", "\\u12g4", "\\u12", "\\u", "tail \\u"):
            with self.subTest(escaped):
                with self.assertRaises(ValueError):
                    unescape_string(escaped)

    def test_the_other_escapes_are_untouched(self):
        self.assertEqual(unescape_string("a\\nb\\tc\\\\d\\\"e\\rf"), "a\nb\tc\\d\"e\rf")

    def test_escape_string_round_trips_every_decodable_string(self):
        for s in ("the 429 path \u2014 retry", "\U0001F600", 'say "hi" \\u2014 literal', "a\\nb", "caf\u00e9"):
            with self.subTest(repr(s)):
                self.assertEqual(unescape_string(escape_string(s)), s)


class WriteFormatTests(unittest.TestCase):
    """The write format never moves: what encoded as bytes B before the patch encodes as B after."""

    def test_non_ascii_stays_literal_in_the_output(self):
        for s in ("caf\u00e9", "\u2014", "\U0001F600"):
            with self.subTest(repr(s)):
                self.assertNotIn("\\u", toon_encode({"r": s}))
                self.assertIn(s, toon_encode({"r": s}))

    def test_the_encoded_bytes_of_a_report_are_what_they_were(self):
        data = {"status": "human_review", "task": "Rate limit headers", "reason": "the 429 path — retry"}
        self.assertEqual(toon_encode(data), 'status: human_review\ntask: Rate limit headers\nreason: the 429 path — retry')
        self.assertEqual(toon_decode(toon_encode(data))["reason"], "the 429 path — retry")


if __name__ == "__main__":
    unittest.main()
