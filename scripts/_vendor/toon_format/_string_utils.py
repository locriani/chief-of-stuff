# Copyright (c) 2025 TOON Format Organization
# SPDX-License-Identifier: MIT
"""String utilities for TOON encoding and decoding.

This module provides shared string processing functions used by both
the encoder and decoder, following the TOON specification Section 7.1
for escape sequences and quoted string handling.
"""

from .constants import (
    BACKSLASH,
    CARRIAGE_RETURN,
    DOUBLE_QUOTE,
    NEWLINE,
    TAB,
)

HEX_DIGITS = "0123456789abcdefABCDEF"  # the four digits a \uXXXX escape names its character with


def escape_string(value: str) -> str:
    """Escape special characters in a string for encoding.

    Handles backslashes, quotes, newlines, carriage returns, and tabs.
    Per Section 7.1 of the TOON specification.

    Args:
        value: The string to escape

    Returns:
        The escaped string

    Examples:
        >>> escape_string('hello\\nworld')
        'hello\\\\nworld'
        >>> escape_string('say "hello"')
        'say \\\\"hello\\\\"'
    """
    return (
        value.replace(BACKSLASH, BACKSLASH + BACKSLASH)
        .replace(DOUBLE_QUOTE, BACKSLASH + DOUBLE_QUOTE)
        .replace(NEWLINE, BACKSLASH + "n")
        .replace(CARRIAGE_RETURN, BACKSLASH + "r")
        .replace(TAB, BACKSLASH + "t")
    )


def unescape_string(value: str) -> str:
    """Unescape a string by processing escape sequences.

    Handles `\\n`, `\\t`, `\\r`, `\\\\`, `\\"`, and `\\uXXXX` escape sequences.
    Per Section 7.1 of the TOON specification. A `\\uXXXX` escape is four hex
    digits; a surrogate pair decodes to one character, and a lone surrogate
    reads as the replacement character.

    Args:
        value: The string to unescape (without surrounding quotes)

    Returns:
        The unescaped string

    Raises:
        ValueError: If an invalid escape sequence is encountered

    Examples:
        >>> unescape_string('hello\\\\nworld')
        'hello\\nworld'
        >>> unescape_string('say \\\\"hello\\\\"')
        'say "hello"'
    """
    result = ""
    i = 0

    while i < len(value):
        if value[i] == BACKSLASH:
            if i + 1 >= len(value):
                raise ValueError("Invalid escape sequence: backslash at end of string")

            next_char = value[i + 1]
            if next_char == "n":
                result += NEWLINE
                i += 2
                continue
            if next_char == "t":
                result += TAB
                i += 2
                continue
            if next_char == "r":
                result += CARRIAGE_RETURN
                i += 2
                continue
            if next_char == BACKSLASH:
                result += BACKSLASH
                i += 2
                continue
            if next_char == DOUBLE_QUOTE:
                result += DOUBLE_QUOTE
                i += 2
                continue
            if next_char == "u":
                digits = value[i + 2:i + 6]
                if len(digits) != 4 or any(d not in HEX_DIGITS for d in digits):
                    raise ValueError("Invalid escape sequence: \\u must be followed by four hexadecimal digits")
                code = int(digits, 16)
                i += 6
                if 0xD800 <= code <= 0xDBFF:  # a high surrogate waits for its low half as another \uXXXX escape
                    low_digits = value[i + 2:i + 6]
                    if (value[i:i + 2] == BACKSLASH + "u" and len(low_digits) == 4
                            and all(d in HEX_DIGITS for d in low_digits) and 0xDC00 <= int(low_digits, 16) <= 0xDFFF):
                        code = 0x10000 + ((code - 0xD800) << 10) + (int(low_digits, 16) - 0xDC00)
                        i += 6
                if 0xD800 <= code <= 0xDFFF:
                    # A lone surrogate cannot survive as text: replace it, as the launcher's own report writes do
                    # (`utf-8, errors="replace"`), and the report still reads.
                    code = 0xFFFD
                result += chr(code)
                continue

            raise ValueError(f"Invalid escape sequence: \\{next_char}")

        result += value[i]
        i += 1

    return result


def find_closing_quote(content: str, start: int) -> int:
    """Find the index of the closing double quote, accounting for escape sequences.

    Args:
        content: The string to search in
        start: The index of the opening quote

    Returns:
        The index of the closing quote, or -1 if not found

    Examples:
        >>> find_closing_quote('"hello"', 0)
        6
        >>> find_closing_quote('"hello \\\\"world\\\\""', 0)
        17
    """
    i = start + 1
    while i < len(content):
        if content[i] == BACKSLASH and i + 1 < len(content):
            # Skip escaped character
            i += 2
            continue
        if content[i] == DOUBLE_QUOTE:
            return i
        i += 1
    return -1  # Not found


def find_unquoted_char(content: str, char: str, start: int = 0) -> int:
    """Find the index of a specific character outside of quoted sections.

    Args:
        content: The string to search in
        char: The character to look for
        start: Optional starting index (defaults to 0)

    Returns:
        The index of the character, or -1 if not found outside quotes

    Examples:
        >>> find_unquoted_char('key: "value: nested"', ':', 0)
        3
        >>> find_unquoted_char('"key: nested": value', ':', 0)
        13
    """
    in_quotes = False
    i = start

    while i < len(content):
        if content[i] == BACKSLASH and i + 1 < len(content) and in_quotes:
            # Skip escaped character
            i += 2
            continue

        if content[i] == DOUBLE_QUOTE:
            in_quotes = not in_quotes
            i += 1
            continue

        if content[i] == char and not in_quotes:
            return i

        i += 1

    return -1
