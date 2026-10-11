"""
Source spans — tying a requirement to the PDR text it came from.

The driving LLM supplies a verbatim QUOTE. CIP finds it in the stored
text and computes the offsets itself. LLMs miscount characters, so an
LLM-supplied offset is never trusted; a quote that cannot be found is
refused.

Also computes span coverage: which sections of the PDR have no
requirement mapped to them. That is the denominator `coverage()` was
missing — measuring against extracted requirements alone reports 100%
even when a third of the PDR was never extracted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MIN_QUOTE_CHARS = 8
# A quote is the sentence that states a requirement, not a passage. Without
# a ceiling one "requirement" could quote the whole document and every
# section would count as mapped.
MAX_QUOTE_CHARS = 600
# Nothing longer than this is a heading. It also keeps every heading
# pattern linear: a 16,000-character line of #s once took seconds.
MAX_HEADING_LINE = 200
# A mapped section this long whose quotes cover less than this share of it
# is reported as lightly quoted.
LIGHT_SECTION_MIN_CHARS = 300
LIGHT_SECTION_MAX_PCT = 20

_WS = re.compile(r"\s+")
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(\S.*)$")
_QUOTED = re.compile(r"^[ \t]{0,3}>[ \t]?")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


@dataclass(frozen=True)
class Span:
    start: int
    end: int                # exclusive


@dataclass(frozen=True)
class Section:
    index: int
    heading: str
    level: int
    start: int
    end: int                # exclusive
    body_start: int         # first character after the heading line
    has_body: bool          # false for a heading with nothing under it


# ---------------------------------------------------------------------------
# Quote location
# ---------------------------------------------------------------------------

class QuoteLocator:
    """
    Finds quotes in one text. Build it once per document: the
    whitespace-collapsed copy and the section list are computed on first
    use and kept, so locating 500 quotes in a 2 MB PDR does that work
    once and not 500 times.
    """

    def __init__(self, text: str | None):
        self.text = text or ""
        self._collapsed: tuple[str, list[int]] | None = None
        self._sections: tuple[Section, ...] | None = None

    def locate(self, quote: str, start_hint: int | None = None) -> Span | None:
        """
        The span of `quote`, or None.

        Exact match first. If that fails, match with runs of whitespace
        collapsed, because an LLM re-flows line breaks and indentation
        when it copies a sentence. Wording and case must still match.

        When the quote occurs more than once, the occurrence nearest
        `start_hint` wins; with no hint, the first.
        """
        text = self.text
        if not text or not isinstance(quote, str):
            return None
        q = quote.strip()
        if len(_WS.sub(" ", q)) < MIN_QUOTE_CHARS:
            return None

        exact = _find_all(text, q)
        if exact:
            start = _pick(exact, start_hint)
            return Span(start, start + len(q))

        if self._collapsed is None:
            self._collapsed = _collapse_whitespace(text)
        norm_text, index_map = self._collapsed
        norm_quote = _WS.sub(" ", q)
        hits = _find_all(norm_text, norm_quote)
        if not hits:
            return None
        # Map the normalised hit back to original offsets.
        candidates = [
            (index_map[h], index_map[h + len(norm_quote) - 1] + 1) for h in hits
        ]
        chosen = _pick([c[0] for c in candidates], start_hint)
        for s, e in candidates:
            if s == chosen:
                return Span(s, e)
        return None

    @property
    def sections(self) -> tuple[Section, ...]:
        if self._sections is None:
            self._sections = split_sections(self.text)
        return self._sections

    def heading_inside(self, span: Span) -> str | None:
        """
        The heading of a section that starts inside `span`, or None. A
        quote that runs across a heading covers more than one section,
        and would mark each of them as mapped.
        """
        for sec in self.sections:
            if span.start < sec.start < span.end:
                return sec.heading
        return None


def locate_quote(text: str, quote: str, start_hint: int | None = None) -> Span | None:
    """One-off lookup. For many quotes in one text, use a QuoteLocator."""
    return QuoteLocator(text).locate(quote, start_hint)


def _find_all(haystack: str, needle: str) -> list[int]:
    out: list[int] = []
    i = haystack.find(needle)
    while i != -1:
        out.append(i)
        i = haystack.find(needle, i + 1)
    return out


def _pick(starts: list[int], hint: int | None) -> int:
    if hint is None:
        return starts[0]
    return min(starts, key=lambda s: (abs(s - hint), s))


def _collapse_whitespace(text: str) -> tuple[str, list[int]]:
    """
    Collapse whitespace runs to one space. Returns (collapsed, index_map)
    where index_map[i] is the original offset of collapsed[i].
    """
    out: list[str] = []
    index_map: list[int] = []
    in_ws = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if not in_ws:
                out.append(" ")
                index_map.append(i)
                in_ws = True
        else:
            out.append(ch)
            index_map.append(i)
            in_ws = False
    return "".join(out), index_map


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def split_sections(text: str) -> tuple[Section, ...]:
    """
    Split a PDR into sections.

    A section starts at anything that reads as a heading. PDRs arrive in
    every layout, and missing a heading is the expensive mistake: text
    under an unrecognised heading is folded into the section above, which
    then looks "mapped" and hides whatever was never extracted. So the
    recognisers are deliberately generous — a false heading only adds a
    section for the human to glance at.

    Recognised, outside fenced code blocks:
      - ATX headings            # Title  ..  ###### Title
      - underlined headings     Title    Title    Title    Title
                                =====    -----    ~~~~~    ^^^^^
                                (and rows of *, + or double quotes)
      - HTML and AsciiDoc headings          <h2>Title</h2>     == Title
      - a line that is only bold text       **Security**
      - a short ALL-CAPS line set off by a blank line
      - a short numbered or lettered title set off by a blank line
                                            2.3 Billing        A. Compliance
      - any other short line with no closing punctuation that stands
        between a blank line and a body: Section 2: Security

    Text before the first heading is a "(preamble)" section. If all of
    that still finds fewer than two sections in a document with several
    paragraphs, the paragraphs themselves become the sections, so there
    is always a denominator.
    """
    if not text:
        return ()

    lines = text.split("\n")
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1                      # the "\n" split() removed

    # A PDR pasted out of an email arrives with every line quoted ("> ").
    # Headings are looked for in the lines without that prefix; offsets are
    # still those of the text as stored.
    written = [ln for ln in lines if ln.strip()]
    if len(written) >= 3 and sum(1 for ln in written if _QUOTED.match(ln)) * 10 >= len(written) * 9:
        lines = [_QUOTED.sub("", ln, count=1) for ln in lines]

    in_code = _fenced_lines(lines)

    def blank(i: int) -> bool:
        return i < 0 or i >= len(lines) or not lines[i].strip()

    heads: list[tuple[int, int, int, str]] = []   # (start, body_start, level, heading)
    skip_next = False
    for i, line in enumerate(lines):
        if skip_next:
            skip_next = False
            continue
        if in_code[i] or not line.strip() or len(line) > MAX_HEADING_LINE:
            continue
        level, heading, consumed = _heading_at(lines, i, in_code, blank)
        if level is None:
            continue
        last = i + consumed - 1
        body_start = min(offsets[last + 1] if last + 1 < len(offsets) else len(text), len(text))
        heads.append((offsets[i], body_start, level, heading))
        skip_next = consumed == 2

    if len(heads) < 2:
        # Nothing read as a heading. Numbered titles with no blank line
        # above them ("1. Scope" straight under the previous paragraph)
        # look like a list, so they are not taken as headings above; but
        # when they are all there is, they are the document's structure.
        numbered = []
        for i, line in enumerate(lines):
            if in_code[i] or len(line) > MAX_HEADING_LINE:
                continue
            m = _NUMBERED.match(line)
            if (m and m.group("t").strip()[-1:] not in ".;!?," and len(m.group("t").split()) <= 10
                    and i + 1 < len(lines) and lines[i + 1].strip()
                    and not _NUMBERED.match(lines[i + 1])):
                body_start = min(offsets[i + 1], len(text))
                numbered.append((offsets[i], body_start, 7, line.strip().rstrip(":").strip()))
        if len(numbered) >= 2:
            heads = sorted(set(heads) | set(numbered))

    sections: list[Section] = []
    if heads:
        if _has_content(text[: heads[0][0]]):
            sections.append(Section(
                index=0, heading="(preamble)", level=0,
                start=0, end=heads[0][0], body_start=0, has_body=True,
            ))
        for i, (start, body_start, level, heading) in enumerate(heads):
            end = heads[i + 1][0] if i + 1 < len(heads) else len(text)
            sections.append(Section(
                index=len(sections), heading=heading, level=level,
                start=start, end=end, body_start=body_start,
                has_body=_has_content(text[body_start:end]),
            ))

    if sum(1 for sec in sections if sec.has_body) < 2:
        paragraphs = _paragraph_sections(text)
        if len(paragraphs) >= 3 or not sections:
            return paragraphs
    return tuple(sections)


def _fenced_lines(lines: list[str]) -> list[bool]:
    """
    Which lines are inside a fenced code block, by CommonMark's rules: a
    fence is three or more backticks or tildes; it is closed by a fence of
    the same character, at least as long, with nothing after it. So a
    four-backtick block can show a three-backtick fence without ending.

    Headings are not looked for inside code. That makes a wrong answer
    here expensive in one direction only: text taken for code hides its
    headings. So when the fences do not add up — one is left open at the
    end of the document — none of them is trusted and nothing is hidden.
    A stray fence then costs a few false headings from code comments,
    not a run of real sections folded into the one above.
    """
    in_code = [False] * len(lines)
    blocks: list[tuple[int, int]] = []
    open_i: int | None = None
    char, length = "", 0
    for i, line in enumerate(lines):
        m = _FENCE.match(line) if len(line) <= MAX_HEADING_LINE else None
        if not m:
            continue
        fence, info = m.group(1), m.group(2).strip()
        if open_i is None:
            if fence[0] == "`" and "`" in info:
                continue                          # inline code, not a fence
            if fence[0] == "~" and not info:
                # A bare row of tildes is an underline in reStructuredText
                # and a divider in plenty of hand-written documents. Read as
                # a fence, a pair of dividers hides every heading between
                # them. Only a tilde fence that names a language is code.
                continue
            open_i, char, length = i, fence[0], len(fence)
        elif fence[0] == char and len(fence) >= length and not info:
            blocks.append((open_i, i))
            open_i = None
        elif fence[0] == char and len(fence) >= length and info:
            # "```json" while a block is open. A fence that names a language
            # can only open one, so the earlier opener was never closed. It
            # is dropped, and the block starts here. Left alone, the stray
            # fence would pair with this block's closer and every later
            # block would be read inside out.
            open_i, char, length = i, fence[0], len(fence)
    if open_i is not None:
        return in_code
    for a, b in blocks:
        for i in range(a, b + 1):
            in_code[i] = True
    return in_code


def _has_content(chunk: str) -> bool:
    """True if there is something to map: letters or digits, not just rules."""
    return any(ch.isalnum() for ch in chunk)


_SETEXT_1 = re.compile(r"^=+[ \t]*$")
_SETEXT_2 = re.compile(r"^-{3,}[ \t]*$")
_SETEXT_3 = re.compile(r"^(~{3,}|\^{3,}|\*{3,}|\+{3,}|\"{3,})[ \t]*$")
_HTML_HEADING = re.compile(r"^[ \t]*<h([1-6])\b[^>]{0,80}>(?P<t>.{1,150}?)</h\1>[ \t]*$", re.I)
_ASCIIDOC = re.compile(r"^(={1,6})[ \t]+(?P<t>\S.{0,150})$")
_LETTERED = re.compile(r"^[ \t]*[A-Z][.)][ \t]+(?P<t>[A-Z].{0,78})$")
_TABLE_OR_QUOTE = re.compile(r"^[ \t]*[|>]")
_BOLD_LINE = re.compile(r"^[ \t]*(\*\*|__)(?P<t>[^*_].{0,98}?)(\*\*|__)[ \t]*:?[ \t]*$")
_NUMBERED = re.compile(r"^[ \t]*\d+(?:\.\d+)*[.)]?[ \t]+(?P<t>[A-Z].{0,78})$")
_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+")


def _atx(line: str) -> tuple[int, str] | None:
    m = _HEADING.match(line)
    if not m:
        return None
    title = m.group(2).rstrip()
    # A closing run of #s is decoration, when a space sets it off.
    bare = title.rstrip("#")
    if bare != title and (not bare or bare[-1] in " \t"):
        title = bare.rstrip()
    return len(m.group(1)), title or m.group(2).strip()


def _heading_at(lines, i, in_code, blank):
    """(level, heading, lines consumed) if line i starts a section, else (None, '', 0)."""
    line = lines[i]
    atx = _atx(line)
    if atx:
        return atx[0], atx[1], 1

    stripped = line.strip()
    # A text line underlined with ===, ---, ~~~ or ^^^.
    if (i + 1 < len(lines) and not in_code[i + 1] and not _LIST_ITEM.match(line)
            and len(lines[i + 1]) <= MAX_HEADING_LINE):
        under = lines[i + 1]
        if _SETEXT_1.match(under) and len(under.strip()) >= 2:
            return 1, stripped, 2
        if _SETEXT_2.match(under) and not _SETEXT_2.match(line):
            return 2, stripped, 2
        # reStructuredText underlines. They must run at least as long as the
        # title, which also keeps a "***" rule under a paragraph from
        # turning its last line into a heading.
        if (_SETEXT_3.match(under) and not _SETEXT_3.match(line)
                and len(under.strip()) >= len(stripped)):
            return 3, stripped, 2

    m = _HTML_HEADING.match(line)
    if m:
        return int(m.group(1)), re.sub(r"<[^>]{0,80}>", "", m.group("t")).strip() or stripped, 1
    m = _ASCIIDOC.match(line)
    if m:
        return len(m.group(1)), m.group("t").strip(), 1

    m = _BOLD_LINE.match(line)
    if m:
        return 7, m.group("t").strip().rstrip(":").strip(), 1

    if blank(i - 1):
        letters = [ch for ch in stripped if ch.isalpha()]
        if (3 <= len(stripped) <= 60 and len(letters) >= 3
                and all(ch.isupper() for ch in letters)
                and stripped[-1] not in ".;!?"):
            return 7, stripped.rstrip(":").strip(), 1
        m = _NUMBERED.match(line) or _LETTERED.match(line)
        if m:
            title = m.group("t").strip()
            if title[-1] not in ".;!?," and len(title.split()) <= 10:
                return 7, stripped.rstrip(":").strip(), 1
        # Anything else that is shaped like a title: a short line with no
        # closing punctuation, a blank line above it and a body directly
        # under it. "Section 2: Security", "Billing (phase 2)". This will
        # sometimes pick up a line that is not a heading; that costs the
        # reader one extra entry in a list, where a missed heading costs
        # them a section they never see.
        first_alpha = next((ch for ch in stripped if ch.isalpha()), "")
        if (3 <= len(stripped) <= 60 and len(stripped.split()) <= 8
                and first_alpha.isupper()
                and stripped[-1] not in ".;!?,"
                and not _LIST_ITEM.match(line) and not _TABLE_OR_QUOTE.match(line)
                and not blank(i + 1)
                and (len(lines[i + 1].strip()) > len(stripped)
                     or lines[i + 1].rstrip()[-1:] in ".;:!?")
                and not _LIST_ITEM.match(lines[i + 1])
                and not _TABLE_OR_QUOTE.match(lines[i + 1])):
            return 8, stripped.rstrip(":").strip(), 1

    return None, "", 0


def _paragraph_sections(text: str) -> tuple[Section, ...]:
    sections: list[Section] = []
    for m in re.finditer(r"\S(?:.*?\S)?(?=\n[ \t]*\n|\s*\Z)", text, flags=re.S):
        start, end = m.start(), m.end()
        chunk = text[start:end]
        # A paragraph that is only a heading line, or only punctuation,
        # holds nothing a requirement could map to.
        first_line = chunk.split("\n", 1)[0]
        if not _has_content(chunk) or ("\n" not in chunk
                                       and len(first_line) <= MAX_HEADING_LINE
                                       and _HEADING.match(chunk)):
            continue
        first_line = first_line.strip()
        heading = first_line if len(first_line) <= 60 else first_line[:57] + "..."
        sections.append(Section(
            index=len(sections), heading=heading, level=0,
            start=start, end=end, body_start=start, has_body=True,
        ))
    return tuple(sections)


# ---------------------------------------------------------------------------
# Span coverage
# ---------------------------------------------------------------------------

def span_coverage(
    text: str,
    spans: list[Span],
    out_of_scope_spans: list[Span] | None = None,
    sections: tuple[Section, ...] | None = None,
) -> dict:
    """
    Report which sections of the PDR have a requirement mapped to them.

    `spans` are the quotes of requirements that are in the build (or not
    yet dispositioned). `out_of_scope_spans` are the quotes of deferred
    and not_applicable ones. A section is:

      mapped                  at least one in-build quote overlaps its body
      out_of_scope_only       only deferred / not_applicable quotes do —
                              listed separately, because marking a note
                              "not applicable" should not be a way to make
                              a section look read
      unmapped                nothing quotes it

    Heading-only sections (a parent heading directly followed by a
    sub-heading) are not counted either way.

    quoted_char_pct is informational. Quotes are short, so a low figure
    is normal; the section lists are what matter.
    """
    if sections is None:
        sections = split_sections(text)

    def clean(items):
        return sorted(
            (s for s in (items or []) if 0 <= s.start < s.end <= len(text)),
            key=lambda s: (s.start, s.end),
        )

    valid, out_valid = clean(spans), clean(out_of_scope_spans)

    def touches(sec: Section, items) -> bool:
        return any(sp.start < sec.end and sp.end > sec.body_start for sp in items)

    def quoted_share(sec: Section) -> tuple[int, int]:
        """(characters of the body that are quoted, visible characters in the body)."""
        body = text[sec.body_start:sec.end]
        visible = sum(1 for ch in body if not ch.isspace())
        covered = 0
        last = sec.body_start
        # In-build quotes only. A not-applicable note that quotes four rows
        # of a table says nothing about whether the table was read for
        # requirements.
        for sp in valid:
            a, b = max(sp.start, last), min(sp.end, sec.end)
            if b > a:
                covered += sum(1 for ch in text[a:b] if not ch.isspace())
                last = b
        return covered, visible

    rows: list[dict] = []
    for sec in sections:
        mapped = touches(sec, valid)
        covered, visible = quoted_share(sec) if sec.has_body else (0, 0)
        rows.append({
            "index": sec.index,
            "heading": sec.heading,
            "level": sec.level,
            "start": sec.start,
            "end": sec.end,
            "has_body": sec.has_body,
            "mapped": mapped,
            "out_of_scope_only": (not mapped) and touches(sec, out_valid),
            "body_chars": visible,
            "quoted_pct": int(round(100 * covered / visible)) if visible else 0,
        })

    counted = [r for r in rows if r["has_body"]]

    def brief(r: dict) -> dict:
        return {"heading": r["heading"], "level": r["level"],
                "start": r["start"], "end": r["end"]}

    # Union length of all quoted spans.
    quoted = 0
    cur_start, cur_end = None, None
    for sp in sorted(valid + out_valid, key=lambda s: (s.start, s.end)):
        if cur_end is None or sp.start > cur_end:
            if cur_end is not None:
                quoted += cur_end - cur_start
            cur_start, cur_end = sp.start, sp.end
        else:
            cur_end = max(cur_end, sp.end)
    if cur_end is not None:
        quoted += cur_end - cur_start

    return {
        "section_count": len(counted),
        "mapped_section_count": sum(1 for r in counted if r["mapped"]),
        "unmapped_sections": [
            brief(r) for r in counted if not r["mapped"] and not r["out_of_scope_only"]
        ],
        "out_of_scope_only_sections": [brief(r) for r in counted if r["out_of_scope_only"]],
        # A section counts as mapped as soon as one quote touches it. A long
        # section — a table of twenty requirements — is then "mapped" by a
        # single row. These are the mapped sections where the quotes cover
        # little of the text, for a person to look at again.
        "lightly_quoted_sections": [
            {**brief(r), "quoted_pct": r["quoted_pct"]}
            for r in counted
            if r["mapped"] and r["body_chars"] >= LIGHT_SECTION_MIN_CHARS
            and r["quoted_pct"] < LIGHT_SECTION_MAX_PCT
        ],
        "quoted_chars": quoted,
        "total_chars": len(text),
        "quoted_char_pct": min(100, int(round(100 * quoted / len(text)))) if text else 0,
        "sections": rows,
    }
