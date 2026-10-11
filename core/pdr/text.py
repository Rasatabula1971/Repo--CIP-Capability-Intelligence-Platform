"""
Text hygiene for anything the driving LLM supplies.

Two separate concerns:

Storage. PostgreSQL text cannot hold a NUL, and a lone surrogate cannot
be encoded as UTF-8. Either one would surface as a database exception
deep inside a tool. bad_text_reason() finds them at the door so the tool
can answer {"error": ...} instead.

Display. The approval summaries are printed in a human's terminal just
before they type a confirmation code. Text inside them is supplied by
the agent whose work is being approved. Left raw, an escape sequence in
a "risk" note can erase the warning lines above it, and a newline in a
"reason" can forge a whole section. one_line() makes any such field a
single line of printable characters before it is rendered.
"""
from __future__ import annotations

import math
import unicodedata
from typing import Any

_MAX_DEPTH = 12

# Categories that never belong in a rendered field: control, format
# (zero-width, bidi), surrogate, private use, unassigned, line and
# paragraph separators.
_UNSAFE_CATEGORIES = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")

# Characters with a harmless category that nevertheless draw as nothing.
# A run of them pads a field out to the terminal's width, so whatever
# follows starts on a line of its own and reads as a line CIP wrote.
_BLANK_GLYPHS = frozenset(
    "\u2800"          # braille pattern blank
    "\u3164"          # hangul filler
    "\u115f\u1160"    # hangul choseong / jungseong filler
    "\uffa0"          # halfwidth hangul filler
    "\u034f"          # combining grapheme joiner
    "\u17b4\u17b5"    # khmer inherent vowels (invisible)
    "\u180e"          # mongolian vowel separator
    "\U00016fe4"      # khitan small script filler
    "\U0001d159"      # musical symbol null notehead
)

# Code points Unicode calls default-ignorable: a renderer draws nothing for
# them. Most are category Cf and caught above. The rest are variation
# selectors and the like, category Mn, which is otherwise a legitimate
# class (accents). Inside a word they change nothing a person can see and
# everything a string comparison does: "oau\ufe0eth" displays as "oauth"
# and matches nothing.
_DEFAULT_IGNORABLE = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160),
    (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)

# Prefix of every continuation row when a long line is wrapped for a
# terminal. Nothing CIP writes starts a line this way.
WRAP_PREFIX = "    | "


def invisible(ch: str) -> bool:
    """True if `ch` draws nothing, or drives the terminal instead of drawing."""
    if ch in _BLANK_GLYPHS or unicodedata.category(ch) in _UNSAFE_CATEGORIES:
        return True
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _DEFAULT_IGNORABLE)


def bad_text_reason(value: Any, _depth: int = 0) -> str | None:
    """
    Why `value` cannot be stored, or None if it can. Walks dicts and
    lists. Checks strings for NUL and lone surrogates, and numbers for
    NaN / infinity (which JSONB refuses).
    """
    if _depth > _MAX_DEPTH:
        return "input is nested too deeply"
    if isinstance(value, str):
        if "\x00" in value:
            return "text contains a NUL character"
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return "text contains an invalid Unicode surrogate"
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return "numbers must be finite"
    if isinstance(value, dict):
        for k, v in value.items():
            r = bad_text_reason(k, _depth + 1) or bad_text_reason(v, _depth + 1)
            if r:
                return r
    elif isinstance(value, (list, tuple)):
        for v in value:
            r = bad_text_reason(v, _depth + 1)
            if r:
                return r
    return None


def first_bad(*values: Any) -> str | None:
    for v in values:
        r = bad_text_reason(v)
        if r:
            return r
    return None


def one_line(text: Any, limit: int | None = None) -> str:
    """
    Render-safe single line: every control, format, surrogate or
    line/paragraph-separator character becomes a space, whitespace is
    collapsed, and the result is optionally truncated.
    """
    if text is None:
        return ""
    s = str(text)
    out = []
    for ch in s:
        out.append(" " if invisible(ch) else ch)
    flat = " ".join("".join(out).split())
    if limit is not None and len(flat) > limit:
        return flat[: max(limit - 3, 0)] + "..."
    return flat


def is_plain_line(text: Any) -> bool:
    """
    True if `text` is already one line of visible characters: no line
    break, tab, escape, zero-width or bidi character, no blank glyph.

    one_line() repairs a field for display. This refuses one at the door,
    for the few fields (a project name, a document title) that are keys
    and headings and so should never need repairing.
    """
    if not isinstance(text, str):
        return False
    if any(invisible(ch) for ch in text):
        return False
    return not is_blank(text)


def printable(text: str) -> str:
    """
    A multi-line block safe to print: newlines and tabs are kept, every
    other control or format character is removed. Used as a last guard on
    a whole rendered summary.
    """
    out = []
    for ch in text:
        if ch in ("\n", "\t"):
            out.append(ch)
            continue
        if ch in _BLANK_GLYPHS:
            out.append(" ")
            continue
        if invisible(ch):
            continue
        out.append(ch)
    return "".join(out)


def display_width(ch: str) -> int:
    """
    The most terminal columns `ch` can take: 2 or 1.

    Wide and fullwidth characters take two columns. So do "ambiguous"
    ones (accented Latin, Greek, Cyrillic, dashes, curly quotes) in a
    terminal set up for an East Asian locale, and emoji in most. This
    rounds every doubtful case up. Counting too many columns wraps a line
    early, which costs nothing; counting too few lets the terminal wrap
    it, which is what wrap_block exists to prevent. A field padded with
    fullwidth letters used to do exactly that: half the characters the
    wrap allowed for, twice the columns.
    """
    if unicodedata.east_asian_width(ch) in ("W", "F", "A") or ord(ch) >= 0x1F000:
        return 2
    return 1


def wrap_block(text: str, width: int) -> str:
    """
    Hard-wrap a block for a terminal `width` columns wide.

    one_line() keeps an agent-written field on one LOGICAL line. The
    terminal then wraps it, and whatever lands at the start of a wrapped
    row looks like a line of its own: pad a field to the right length and
    "## Reviewed and pre-approved by security" appears under it. So CIP
    does the wrapping itself, and marks every continuation row with
    WRAP_PREFIX. A forged row can then only ever appear behind that mark.

    Rows are measured in terminal columns (display_width), not characters.
    """
    width = max(int(width), len(WRAP_PREFIX) + 20)
    out: list[str] = []
    for line in text.split("\n"):
        row: list[str] = []
        used, limit, prefix = 0, width, ""
        for ch in line.expandtabs(4):
            w = display_width(ch)
            if used + w > limit:
                # Break after the last space if there is one in the final
                # stretch of the row, so ordinary prose breaks between
                # words. A row with no such space (a padded field) is cut
                # where it stands. Either way the next row carries the mark.
                carry: list[str] = []
                cut = max((i for i, c in enumerate(row) if c == " "), default=-1)
                if cut >= len(row) * 3 // 5:
                    row, carry = row[:cut + 1], row[cut + 1:]
                out.append(prefix + "".join(row))
                row, used = carry, sum(display_width(c) for c in carry)
                limit, prefix = width - len(WRAP_PREFIX), WRAP_PREFIX
            row.append(ch)
            used += w
        out.append(prefix + "".join(row))
    return "\n".join(out)


def nfc(text: str) -> str:
    """Canonical composition, so 'Å' typed two ways is one name, not two."""
    return unicodedata.normalize("NFC", text)


def is_blank(text: Any) -> bool:
    """
    True if nothing would be visible: empty, whitespace, invisible
    characters, or combining marks with no letter to sit on. A field has
    to contain at least one letter, digit, punctuation mark or symbol.
    """
    return not any(unicodedata.category(ch)[0] in "LNPS" for ch in one_line(text))


# What a search term may be made of, besides letters, digits and spaces.
_TERM_PUNCTUATION = frozenset("+#.-/")


def search_term_problem(term: str) -> str | None:
    """
    Why `term` cannot be a search term, or None if it can.

    A search term is shown to a person, who confirms it, and then sent to
    the registry. The two have to be the same thing. So a term is plain
    ASCII letters, digits, single spaces and a little punctuation ("c++",
    "node.js", "ci/cd"):

      - no comma, semicolon or quote. Terms are listed with commas, so
        "oauth, oidc, sso" as ONE term reads exactly like three terms and
        finds what none of them would
      - no % or _, which the registry's LIKE search treats as wildcards
      - nothing outside ASCII. A word that mixes Latin with a Cyrillic
        look-alike was refused before; a word written ENTIRELY in
        look-alikes was not ("сасһе" in Cyrillic, "oauth" in mathematical
        or fullwidth letters, "ɡraphql" with a script g). There is no
        list of look-alikes that is complete, and a term the reader takes
        for "cache" that matches nothing is "the registry has nothing".
        The registry's names and keys are ASCII, so that is what a term
        is written in.
    """
    if mixed_script(term):
        return "mixes alphabets inside a word (Latin with Cyrillic or Greek look-alikes)"
    for ch in term:
        if invisible(ch):
            return f"contains an invisible character (U+{ord(ch):04X})"
    for ch in term:
        if ch == " " or (ch.isascii() and (ch.isalnum() or ch in _TERM_PUNCTUATION)):
            continue
        if not ch.isascii():
            return (f"contains U+{ord(ch):04X} ({unicodedata.name(ch, 'unnamed character').lower()}); "
                    f"a term is plain ASCII letters, digits, spaces and "
                    f"{' '.join(sorted(_TERM_PUNCTUATION))}, so that it cannot be "
                    f"a look-alike of another term")
        shown = ch if ch.isprintable() else f"U+{ord(ch):04X}"
        return (f"contains {shown!r}; a term is plain ASCII letters, digits, spaces and "
                f"{' '.join(sorted(_TERM_PUNCTUATION))}")
    return None


def non_ascii_letters(text: Any) -> bool:
    """
    True if `text` has a letter or digit outside ASCII. Such a name cannot
    be checked against a list of ASCII names: "ОКТА" in Cyrillic and
    "Strıpe" with a dotless i read as the names they imitate and match
    nothing.
    """
    return any(not ch.isascii() and unicodedata.category(ch)[0] in "LN"
               for ch in str(text or ""))


def fold_name(name: Any) -> str:
    """
    A name reduced to its letters and digits, lower-cased, compatibility
    forms folded, marks dropped: "Str\ufe0eipe", "ＳＴＲＩＰＥ" and "s t r i p e"
    all come out "stripe". For matching a name against a list, where any
    decoration is a way round the list.
    """
    text = unicodedata.normalize("NFKD", str(name or "")).casefold()
    return "".join(ch for ch in text if unicodedata.category(ch)[0] in "LN" and
                   unicodedata.category(ch) != "Lm")


_SCRIPTS = ("LATIN", "CYRILLIC", "GREEK")


def mixed_script(text: str) -> bool:
    """
    True if one word mixes Latin letters with Cyrillic or Greek ones.
    "оauth" with a Cyrillic о looks identical to "oauth" on screen and
    matches nothing in the registry.
    """
    for word in text.split():
        seen = set()
        for ch in word:
            if not ch.isalpha():
                continue
            name = unicodedata.name(ch, "")
            for script in _SCRIPTS:
                if name.startswith(script):
                    seen.add(script)
        if "LATIN" in seen and len(seen) > 1:
            return True
    return False


def too_long(value: Any, limit: int) -> bool:
    return isinstance(value, str) and len(value) > limit
