"""
TypeScript / JavaScript interface extractor.

Regex-based parser (no tree-sitter, no external deps) that emits
'interface' evidence for exported functions, classes, interfaces,
and type aliases in .ts, .tsx, .js, and .jsx files.

'Exported' = prefixed with ``export`` (including ``export default``).
Non-exported top-level declarations are skipped — mirrors the Python
convention of underscore-prefixed names being private.

Limitations inherent to regex parsing:
  - Nested braces in signatures may truncate parameter lists.
  - Template-heavy generics can confuse the return-type capture.
  - Decorators are detected by leading ``@`` but not fully parsed.
Good enough for capability intelligence; tree-sitter can refine later.
"""
from __future__ import annotations

import re
from typing import Iterable, Iterator

from analysis.evidence import (
    LOCATOR_FILE_RANGE,
    EvidenceItem,
    SourceFile,
)

_TS_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"}

_EXPORT_FUNCTION_RE = re.compile(
    r"^(?P<exp>export\s+(?:default\s+)?)?(?P<async>async\s+)?function\s+"
    r"(?P<name>\w+)\s*(?:<[^>]*>\s*)?\((?P<params>[^)]*)\)"
    r"(?:\s*:\s*(?P<ret>[^\s{]+(?:\s*\|\s*[^\s{]+)*))?"
    r"\s*\{",
    re.MULTILINE,
)

_EXPORT_ARROW_RE = re.compile(
    r"^export\s+(?:default\s+)?(?:const|let|var)\s+"
    r"(?P<name>\w+)\s*"
    r"(?::\s*[^\s=]+\s*)?"
    r"=\s*(?P<async>async\s+)?"
    r"(?:\([^)]*\)|(?P<single_param>\w+))"
    r"(?:\s*:\s*(?P<ret>[^\s=>{]+))?"
    r"\s*=>",
    re.MULTILINE,
)

_EXPORT_CLASS_RE = re.compile(
    r"^(?P<exp>export\s+(?:default\s+)?)?(?:abstract\s+)?"
    r"class\s+(?P<name>\w+)\s*"
    r"(?:<[^>]*>\s*)?"
    r"(?:extends\s+(?P<base>[^\s{]+)\s*)?"
    r"(?:implements\s+(?P<ifaces>[^{]+))?"
    r"\s*\{",
    re.MULTILINE,
)

_EXPORT_INTERFACE_RE = re.compile(
    r"^export\s+(?:default\s+)?"
    r"interface\s+(?P<name>\w+)\s*"
    r"(?:<[^>]*>\s*)?"
    r"(?:extends\s+(?P<base>[^{]+))?"
    r"\s*\{",
    re.MULTILINE,
)

_EXPORT_TYPE_RE = re.compile(
    r"^export\s+(?:default\s+)?"
    r"type\s+(?P<name>\w+)\s*"
    r"(?:<[^>]*>\s*)?"
    r"\s*=\s*(?P<def>[^;]+)",
    re.MULTILINE,
)

_METHOD_RE = re.compile(
    r"^\s+(?:(?:public|protected|private|static|readonly|abstract|async|override|get|set)\s+)*"
    r"(?P<name>\w+)\s*(?:<[^>]*>\s*)?\([^)]*\)",
    re.MULTILINE,
)

_INTERFACE_MEMBER_RE = re.compile(
    r"^\s+(?:readonly\s+)?(?P<name>\w+)\s*(?:\??\s*:\s*|\s*\()",
    re.MULTILINE,
)


def _ext(path: str) -> str:
    dot = path.rfind(".")
    return path[dot:].lower() if dot >= 0 else ""


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _end_of_block(text: str, open_pos: int) -> int:
    depth = 0
    i = open_pos
    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(text) - 1


class TSInterfaceExtractor:
    name = "ts_interfaces"

    def extract(self, files: Iterable[SourceFile]) -> Iterator[EvidenceItem]:
        for f in files:
            if _ext(f.path) not in _TS_EXTENSIONS:
                continue
            try:
                text = f.content.decode("utf-8", errors="replace")
            except Exception:
                continue
            yield from self._extract_file(text, f)

    def _extract_file(self, text: str, f: SourceFile) -> Iterator[EvidenceItem]:
        yield from self._extract_functions(text, f)
        yield from self._extract_arrows(text, f)
        yield from self._extract_classes(text, f)
        yield from self._extract_interfaces(text, f)
        yield from self._extract_types(text, f)

    def _extract_functions(self, text: str, f: SourceFile) -> Iterator[EvidenceItem]:
        for m in _EXPORT_FUNCTION_RE.finditer(text):
            if not m.group("exp"):
                continue
            is_async = bool(m.group("async"))
            brace_pos = m.end() - 1
            end_pos = _end_of_block(text, brace_pos)
            start_line = _line_of(text, m.start())
            end_line = _line_of(text, end_pos)
            lang = "typescript" if _ext(f.path) in {".ts", ".tsx", ".mts", ".cts"} else "javascript"
            yield EvidenceItem(
                evidence_type="interface",
                locator_kind=LOCATOR_FILE_RANGE,
                locator={"path": f.path, "start_line": start_line, "end_line": end_line},
                extracted_value={
                    "kind": "async_function" if is_async else "function",
                    "name": m.group("name"),
                    "signature": (m.group("params") or "").strip(),
                    "returns": (m.group("ret") or "").strip(),
                    "language": lang,
                },
            )

    def _extract_arrows(self, text: str, f: SourceFile) -> Iterator[EvidenceItem]:
        for m in _EXPORT_ARROW_RE.finditer(text):
            is_async = bool(m.group("async"))
            start_line = _line_of(text, m.start())
            end_line = start_line
            arrow_pos = text.find("=>", m.start())
            if arrow_pos >= 0:
                after = text[arrow_pos + 2:].lstrip()
                if after.startswith("{"):
                    brace_pos = text.index("{", arrow_pos)
                    end_pos = _end_of_block(text, brace_pos)
                    end_line = _line_of(text, end_pos)
                else:
                    semi = text.find(";", arrow_pos)
                    nl = text.find("\n", arrow_pos)
                    bound = min(
                        semi if semi >= 0 else len(text),
                        nl if nl >= 0 else len(text),
                    )
                    end_line = _line_of(text, bound)
            lang = "typescript" if _ext(f.path) in {".ts", ".tsx", ".mts", ".cts"} else "javascript"
            yield EvidenceItem(
                evidence_type="interface",
                locator_kind=LOCATOR_FILE_RANGE,
                locator={"path": f.path, "start_line": start_line, "end_line": end_line},
                extracted_value={
                    "kind": "async_function" if is_async else "function",
                    "name": m.group("name"),
                    "signature": "",
                    "returns": (m.group("ret") or "").strip(),
                    "language": lang,
                },
            )

    def _extract_classes(self, text: str, f: SourceFile) -> Iterator[EvidenceItem]:
        for m in _EXPORT_CLASS_RE.finditer(text):
            if not m.group("exp"):
                continue
            brace_pos = text.index("{", m.start() + len(m.group()) - 1)
            end_pos = _end_of_block(text, brace_pos)
            start_line = _line_of(text, m.start())
            end_line = _line_of(text, end_pos)
            body = text[brace_pos:end_pos + 1]
            methods = sorted({
                mm.group("name")
                for mm in _METHOD_RE.finditer(body)
                if not mm.group("name").startswith("_")
                and mm.group("name") != "constructor"
                and mm.group("name") not in ("get", "set", "static", "async",
                                               "public", "private", "protected",
                                               "readonly", "abstract", "override")
            })
            bases = []
            if m.group("base"):
                bases.append(m.group("base").strip())
            if m.group("ifaces"):
                bases.extend(
                    b.strip() for b in m.group("ifaces").split(",") if b.strip()
                )
            lang = "typescript" if _ext(f.path) in {".ts", ".tsx", ".mts", ".cts"} else "javascript"
            yield EvidenceItem(
                evidence_type="interface",
                locator_kind=LOCATOR_FILE_RANGE,
                locator={"path": f.path, "start_line": start_line, "end_line": end_line},
                extracted_value={
                    "kind": "class",
                    "name": m.group("name"),
                    "bases": bases,
                    "public_methods": methods,
                    "language": lang,
                },
            )

    def _extract_interfaces(self, text: str, f: SourceFile) -> Iterator[EvidenceItem]:
        for m in _EXPORT_INTERFACE_RE.finditer(text):
            brace_pos = text.index("{", m.start() + len(m.group()) - 1)
            end_pos = _end_of_block(text, brace_pos)
            start_line = _line_of(text, m.start())
            end_line = _line_of(text, end_pos)
            body = text[brace_pos:end_pos + 1]
            members = sorted({
                mm.group("name")
                for mm in _INTERFACE_MEMBER_RE.finditer(body)
                if not mm.group("name").startswith("_")
            })
            bases = []
            if m.group("base"):
                bases = [b.strip() for b in m.group("base").split(",") if b.strip()]
            lang = "typescript" if _ext(f.path) in {".ts", ".tsx", ".mts", ".cts"} else "javascript"
            yield EvidenceItem(
                evidence_type="interface",
                locator_kind=LOCATOR_FILE_RANGE,
                locator={"path": f.path, "start_line": start_line, "end_line": end_line},
                extracted_value={
                    "kind": "interface",
                    "name": m.group("name"),
                    "bases": bases,
                    "members": members,
                    "language": lang,
                },
            )

    def _extract_types(self, text: str, f: SourceFile) -> Iterator[EvidenceItem]:
        for m in _EXPORT_TYPE_RE.finditer(text):
            start_line = _line_of(text, m.start())
            semi = text.find(";", m.end())
            end_line = _line_of(text, semi if semi >= 0 else m.end())
            lang = "typescript" if _ext(f.path) in {".ts", ".tsx", ".mts", ".cts"} else "javascript"
            yield EvidenceItem(
                evidence_type="interface",
                locator_kind=LOCATOR_FILE_RANGE,
                locator={"path": f.path, "start_line": start_line, "end_line": end_line},
                extracted_value={
                    "kind": "type_alias",
                    "name": m.group("name"),
                    "definition": m.group("def").strip().rstrip(";"),
                    "language": lang,
                },
            )
