"""
TypeScript / JavaScript symbol extractor.

Regex-based parser (no tree-sitter, no external deps) that emits
granular 'symbol' evidence for exported declarations in TS/JS files:
functions, async functions, classes (with public methods), interfaces,
type aliases, enums, and top-level constants.

Classifies each symbol by role using naming conventions, decorators,
base classes, and file-path heuristics — the TS/JS analogue of the
Python SymbolExtractor's role classification.

Role classification heuristics (TS/JS-specific):
  component      — React component (PascalCase function returning JSX,
                    or class extending Component/PureComponent)
  hook           — React hook (function named use*)
  api_endpoint   — decorated with @Get/@Post/@Put etc. (NestJS/routing)
  middleware     — named *Middleware or *middleware
  data_model     — interface/type/class used as a shape (DTO, Entity, Schema)
  factory        — function named create*, make*, build*
  guard          — NestJS guard (class implementing CanActivate)
  pipe           — NestJS pipe (class implementing PipeTransform)
  test_helper    — in test/spec files, named mock*, stub*, fake*, assert*
  entry_point    — main(), bootstrap(), default export in index files
  exception      — class extending Error / HttpException
  enum_type      — TypeScript enum
  utility        — default / everything else
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from analysis.evidence import (
    LOCATOR_FILE_RANGE,
    EvidenceItem,
    SourceFile,
)

_TS_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"}

# --- regexes -----------------------------------------------------------------

_FUNCTION_RE = re.compile(
    r"^(?P<exp>export\s+(?:default\s+)?)?(?P<async>async\s+)?function\s+"
    r"(?P<name>\w+)\s*(?:<[^>]*>\s*)?\((?P<params>[^)]*)\)"
    r"(?:\s*:\s*(?P<ret>[^\s{]+(?:\s*\|\s*[^\s{]+)*))?"
    r"\s*\{",
    re.MULTILINE,
)

_ARROW_RE = re.compile(
    r"^(?P<exp>export\s+(?:default\s+)?)?(?:const|let|var)\s+"
    r"(?P<name>\w+)\s*"
    r"(?::\s*[^\s=]+\s*)?"
    r"=\s*(?P<async>async\s+)?"
    r"(?:\([^)]*\)|(?P<single_param>\w+))"
    r"(?:\s*:\s*(?P<ret>[^\s=>{]+))?"
    r"\s*=>",
    re.MULTILINE,
)

_CLASS_RE = re.compile(
    r"^(?P<dec>(?:\s*@\w+(?:\([^)]*\))?\s*\n)*)?"
    r"(?P<exp>export\s+(?:default\s+)?)?(?:abstract\s+)?"
    r"class\s+(?P<name>\w+)\s*"
    r"(?:<[^>]*>\s*)?"
    r"(?:extends\s+(?P<base>[^\s{]+)\s*)?"
    r"(?:implements\s+(?P<ifaces>[^{]+))?"
    r"\s*\{",
    re.MULTILINE,
)

_INTERFACE_RE = re.compile(
    r"^(?P<exp>export\s+(?:default\s+)?)?"
    r"interface\s+(?P<name>\w+)\s*"
    r"(?:<[^>]*>\s*)?"
    r"(?:extends\s+(?P<base>[^{]+))?"
    r"\s*\{",
    re.MULTILINE,
)

_TYPE_RE = re.compile(
    r"^(?P<exp>export\s+(?:default\s+)?)?"
    r"type\s+(?P<name>\w+)\s*"
    r"(?:<[^>]*>\s*)?"
    r"\s*=\s*(?P<def>[^;]+)",
    re.MULTILINE,
)

_ENUM_RE = re.compile(
    r"^(?P<exp>export\s+(?:default\s+)?)?(?:const\s+)?"
    r"enum\s+(?P<name>\w+)\s*\{",
    re.MULTILINE,
)

_CONST_RE = re.compile(
    r"^(?P<exp>export\s+(?:default\s+)?)?(?:const|let|var)\s+"
    r"(?P<name>[A-Z][A-Z0-9_]*)\s*"
    r"(?::\s*(?P<type>[^\s=]+)\s*)?"
    r"=\s*(?P<val>[^;]+)",
    re.MULTILINE,
)

_METHOD_RE = re.compile(
    r"^\s+(?P<decs>(?:@\w+(?:\([^)]*\))?\s*\n\s*)*)"
    r"(?:(?:public|protected|private|static|readonly|abstract|async|override|get|set)\s+)*"
    r"(?P<name>\w+)\s*(?:<[^>]*>\s*)?\((?P<params>[^)]*)\)"
    r"(?:\s*:\s*(?P<ret>[^\s{;]+))?",
    re.MULTILINE,
)

_DECORATOR_RE = re.compile(r"@(\w+)")

# --- helpers -----------------------------------------------------------------

def _ext(path: str) -> str:
    dot = path.rfind(".")
    return path[dot:].lower() if dot >= 0 else ""


def _is_ts(path: str) -> bool:
    return _ext(path) in {".ts", ".tsx", ".mts", ".cts"}


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


def _path_to_module(path: str) -> str:
    p = path.replace("\\", "/")
    for suffix in ("/index.ts", "/index.tsx", "/index.js", "/index.jsx",
                    "/index.mts", "/index.mjs"):
        if p.endswith(suffix):
            p = p[:-len(suffix)]
            return p.replace("/", ".")
    for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"):
        if p.endswith(ext):
            p = p[:-len(ext)]
            break
    return p.replace("/", ".")


def _extract_decorators(text: str) -> set[str]:
    return set(_DECORATOR_RE.findall(text))


# --- role classification ----------------------------------------------------

_API_DECORATORS = {
    "Get", "Post", "Put", "Delete", "Patch", "Head", "Options", "All",
    "get", "post", "put", "delete", "patch",
    "Route", "route", "Controller", "controller",
}

_GUARD_INTERFACES = {"CanActivate", "CanDeactivate"}
_PIPE_INTERFACES = {"PipeTransform"}
_EXCEPTION_BASES = {"Error", "HttpException", "BadRequestException",
                     "NotFoundException", "UnauthorizedException"}
_COMPONENT_BASES = {"Component", "PureComponent", "React.Component",
                     "React.PureComponent"}
_DATA_MODEL_SUFFIXES = ("Dto", "DTO", "Entity", "Schema", "Model", "Props",
                         "State", "Params", "Options", "Config", "Settings")


def _classify_function_role(name: str, decorators: set[str],
                            file_path: str, is_default: bool) -> str:
    if decorators & _API_DECORATORS:
        return "api_endpoint"
    if name.startswith("use") and name[3:4].isupper():
        return "hook"
    if name[0:1].isupper() and _ext(file_path) in {".tsx", ".jsx"}:
        return "component"
    if name.endswith("Middleware") or name.endswith("_middleware"):
        return "middleware"
    if name.startswith(("create", "make", "build")) and name != "create" and name != "make" and name != "build":
        return "factory"
    if name in ("main", "bootstrap", "setup", "init"):
        return "entry_point"
    if is_default and file_path.replace("\\", "/").endswith(
        ("/index.ts", "/index.tsx", "/index.js", "/index.jsx")):
        return "entry_point"
    test_markers = ("test", "spec", "__tests__", "__test__")
    if any(t in file_path.lower() for t in test_markers):
        if name.startswith(("mock", "stub", "fake", "assert", "create")):
            return "test_helper"
    return "utility"


def _classify_class_role(name: str, bases: list[str],
                         ifaces: list[str], decorators: set[str]) -> str:
    if decorators & _API_DECORATORS or "Controller" in decorators:
        return "api_endpoint"
    base_names = {b.split(".")[-1] for b in bases}
    if base_names & _COMPONENT_BASES:
        return "component"
    if base_names & _EXCEPTION_BASES:
        return "exception"
    iface_names = {i.split(".")[-1] for i in ifaces}
    if iface_names & _GUARD_INTERFACES:
        return "guard"
    if iface_names & _PIPE_INTERFACES:
        return "pipe"
    if name.endswith("Middleware"):
        return "middleware"
    if any(name.endswith(s) for s in _DATA_MODEL_SUFFIXES):
        return "data_model"
    return "utility"


def _classify_interface_role(name: str, bases: list[str]) -> str:
    if any(name.endswith(s) for s in _DATA_MODEL_SUFFIXES):
        return "data_model"
    return "utility"


def _classify_method_role(name: str, decorators: set[str]) -> str:
    if decorators & _API_DECORATORS:
        return "api_endpoint"
    return "utility"


# --- dataclass ---------------------------------------------------------------

@dataclass(frozen=True)
class TSSymbolInfo:
    module_path: str
    symbol_kind: str
    symbol_name: str
    qualified_name: str
    signature: str | None = None
    return_type: str | None = None
    role: str = "utility"
    language: str = "typescript"
    methods: list[str] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)


# --- extractor ---------------------------------------------------------------

class TSSymbolExtractor:
    name = "ts_symbols"

    def extract(self, files: Iterable[SourceFile]) -> Iterator[EvidenceItem]:
        for f in files:
            if _ext(f.path) not in _TS_EXTENSIONS:
                continue
            try:
                text = f.content.decode("utf-8", errors="replace")
            except Exception:
                continue
            yield from self._extract_file(text, f)

    def _extract_file(
        self, text: str, f: SourceFile,
    ) -> Iterator[EvidenceItem]:
        module_path = _path_to_module(f.path)
        lang = "typescript" if _is_ts(f.path) else "javascript"
        yield from self._extract_functions(text, f, module_path, lang)
        yield from self._extract_arrows(text, f, module_path, lang)
        yield from self._extract_classes(text, f, module_path, lang)
        yield from self._extract_interfaces(text, f, module_path, lang)
        yield from self._extract_types(text, f, module_path, lang)
        yield from self._extract_enums(text, f, module_path, lang)
        yield from self._extract_constants(text, f, module_path, lang)

    # -- functions ------------------------------------------------------------

    def _extract_functions(
        self, text: str, f: SourceFile, module_path: str, lang: str,
    ) -> Iterator[EvidenceItem]:
        for m in _FUNCTION_RE.finditer(text):
            name = m.group("name")
            is_exported = bool(m.group("exp"))
            if not is_exported and name.startswith("_"):
                continue
            is_async = bool(m.group("async"))
            is_default = "default" in (m.group("exp") or "")
            kind = "async_function" if is_async else "function"
            brace_pos = m.end() - 1
            end_pos = _end_of_block(text, brace_pos)
            role = _classify_function_role(name, set(), f.path, is_default)
            info = TSSymbolInfo(
                module_path=module_path,
                symbol_kind=kind,
                symbol_name=name,
                qualified_name=f"{module_path}.{name}" if module_path else name,
                signature=(m.group("params") or "").strip(),
                return_type=(m.group("ret") or "").strip() or None,
                role=role,
                language=lang,
            )
            yield self._to_evidence(info, m.start(), end_pos, f)

    # -- arrows ---------------------------------------------------------------

    def _extract_arrows(
        self, text: str, f: SourceFile, module_path: str, lang: str,
    ) -> Iterator[EvidenceItem]:
        for m in _ARROW_RE.finditer(text):
            name = m.group("name")
            is_exported = bool(m.group("exp"))
            if not is_exported and name.startswith("_"):
                continue
            is_async = bool(m.group("async"))
            is_default = "default" in (m.group("exp") or "")
            kind = "async_function" if is_async else "function"
            end_pos = m.end()
            arrow_pos = text.find("=>", m.start())
            if arrow_pos >= 0:
                after = text[arrow_pos + 2:].lstrip()
                if after.startswith("{"):
                    bp = text.index("{", arrow_pos)
                    end_pos = _end_of_block(text, bp)
                else:
                    semi = text.find(";", arrow_pos)
                    end_pos = semi if semi >= 0 else m.end()
            role = _classify_function_role(name, set(), f.path, is_default)
            info = TSSymbolInfo(
                module_path=module_path,
                symbol_kind=kind,
                symbol_name=name,
                qualified_name=f"{module_path}.{name}" if module_path else name,
                return_type=(m.group("ret") or "").strip() or None,
                role=role,
                language=lang,
            )
            yield self._to_evidence(info, m.start(), end_pos, f)

    # -- classes --------------------------------------------------------------

    def _extract_classes(
        self, text: str, f: SourceFile, module_path: str, lang: str,
    ) -> Iterator[EvidenceItem]:
        for m in _CLASS_RE.finditer(text):
            name = m.group("name")
            is_exported = bool(m.group("exp"))
            if not is_exported and name.startswith("_"):
                continue
            brace_pos = m.end() - 1
            end_pos = _end_of_block(text, brace_pos)
            body = text[brace_pos:end_pos + 1]
            dec_text = m.group("dec") or ""
            decorators = _extract_decorators(dec_text)
            bases = []
            if m.group("base"):
                bases.append(m.group("base").strip())
            ifaces = []
            if m.group("ifaces"):
                ifaces = [b.strip() for b in m.group("ifaces").split(",") if b.strip()]
            methods = self._extract_methods(body, module_path, name, f, lang)
            method_names = sorted({mi.symbol_name for mi in methods})
            role = _classify_class_role(name, bases, ifaces, decorators)
            info = TSSymbolInfo(
                module_path=module_path,
                symbol_kind="class",
                symbol_name=name,
                qualified_name=f"{module_path}.{name}" if module_path else name,
                role=role,
                language=lang,
                methods=method_names,
                bases=bases + ifaces,
            )
            yield self._to_evidence(info, m.start(), end_pos, f)
            for mi in methods:
                yield self._to_evidence(
                    mi,
                    _find_method_pos(text, brace_pos, mi.symbol_name),
                    end_pos,
                    f,
                )

    def _extract_methods(
        self, body: str, module_path: str, class_name: str,
        f: SourceFile, lang: str,
    ) -> list[TSSymbolInfo]:
        result = []
        skip = {"constructor", "get", "set", "static", "async",
                "public", "private", "protected", "readonly",
                "abstract", "override"}
        for mm in _METHOD_RE.finditer(body):
            mname = mm.group("name")
            if mname.startswith("_") or mname in skip:
                continue
            dec_text = mm.group("decs") or ""
            decorators = _extract_decorators(dec_text)
            qbase = f"{module_path}.{class_name}" if module_path else class_name
            role = _classify_method_role(mname, decorators)
            result.append(TSSymbolInfo(
                module_path=module_path,
                symbol_kind="method",
                symbol_name=mname,
                qualified_name=f"{qbase}.{mname}",
                signature=(mm.group("params") or "").strip(),
                return_type=(mm.group("ret") or "").strip() or None,
                role=role,
                language=lang,
            ))
        return result

    # -- interfaces -----------------------------------------------------------

    def _extract_interfaces(
        self, text: str, f: SourceFile, module_path: str, lang: str,
    ) -> Iterator[EvidenceItem]:
        for m in _INTERFACE_RE.finditer(text):
            name = m.group("name")
            is_exported = bool(m.group("exp"))
            if not is_exported and name.startswith("_"):
                continue
            brace_pos = m.end() - 1
            end_pos = _end_of_block(text, brace_pos)
            bases = []
            if m.group("base"):
                bases = [b.strip() for b in m.group("base").split(",") if b.strip()]
            role = _classify_interface_role(name, bases)
            info = TSSymbolInfo(
                module_path=module_path,
                symbol_kind="interface",
                symbol_name=name,
                qualified_name=f"{module_path}.{name}" if module_path else name,
                role=role,
                language=lang,
                bases=bases,
            )
            yield self._to_evidence(info, m.start(), end_pos, f)

    # -- type aliases ---------------------------------------------------------

    def _extract_types(
        self, text: str, f: SourceFile, module_path: str, lang: str,
    ) -> Iterator[EvidenceItem]:
        for m in _TYPE_RE.finditer(text):
            name = m.group("name")
            is_exported = bool(m.group("exp"))
            if not is_exported and name.startswith("_"):
                continue
            semi = text.find(";", m.end())
            end_pos = semi if semi >= 0 else m.end()
            role = "data_model" if any(
                name.endswith(s) for s in _DATA_MODEL_SUFFIXES
            ) else "utility"
            info = TSSymbolInfo(
                module_path=module_path,
                symbol_kind="type_alias",
                symbol_name=name,
                qualified_name=f"{module_path}.{name}" if module_path else name,
                role=role,
                language=lang,
            )
            yield self._to_evidence(info, m.start(), end_pos, f)

    # -- enums ----------------------------------------------------------------

    def _extract_enums(
        self, text: str, f: SourceFile, module_path: str, lang: str,
    ) -> Iterator[EvidenceItem]:
        for m in _ENUM_RE.finditer(text):
            name = m.group("name")
            is_exported = bool(m.group("exp"))
            if not is_exported and name.startswith("_"):
                continue
            brace_pos = m.end() - 1
            end_pos = _end_of_block(text, brace_pos)
            info = TSSymbolInfo(
                module_path=module_path,
                symbol_kind="enum",
                symbol_name=name,
                qualified_name=f"{module_path}.{name}" if module_path else name,
                role="enum_type",
                language=lang,
            )
            yield self._to_evidence(info, m.start(), end_pos, f)

    # -- constants ------------------------------------------------------------

    def _extract_constants(
        self, text: str, f: SourceFile, module_path: str, lang: str,
    ) -> Iterator[EvidenceItem]:
        for m in _CONST_RE.finditer(text):
            name = m.group("name")
            is_exported = bool(m.group("exp"))
            if not is_exported:
                continue
            info = TSSymbolInfo(
                module_path=module_path,
                symbol_kind="constant",
                symbol_name=name,
                qualified_name=f"{module_path}.{name}" if module_path else name,
                role="utility",
                language=lang,
            )
            semi = text.find(";", m.end())
            end_pos = semi if semi >= 0 else m.end()
            yield self._to_evidence(info, m.start(), end_pos, f)

    # -- evidence builder -----------------------------------------------------

    def _to_evidence(
        self, info: TSSymbolInfo, start_pos: int, end_pos: int,
        f: SourceFile,
    ) -> EvidenceItem:
        text = f.content.decode("utf-8", errors="replace")
        value: dict = {
            "module_path": info.module_path,
            "symbol_kind": info.symbol_kind,
            "symbol_name": info.symbol_name,
            "qualified_name": info.qualified_name,
            "role": info.role,
            "language": info.language,
        }
        if info.signature is not None:
            value["signature"] = info.signature
        if info.return_type:
            value["return_type"] = info.return_type
        if info.methods:
            value["methods"] = info.methods
        if info.bases:
            value["bases"] = info.bases
        return EvidenceItem(
            evidence_type="symbol",
            locator_kind=LOCATOR_FILE_RANGE,
            locator={
                "path": f.path,
                "start_line": _line_of(text, start_pos),
                "end_line": _line_of(text, end_pos),
            },
            extracted_value=value,
        )


def _find_method_pos(text: str, class_brace: int, method_name: str) -> int:
    pat = re.compile(r"\b" + re.escape(method_name) + r"\s*(?:<[^>]*>\s*)?\(")
    m = pat.search(text, class_brace)
    if m:
        line_start = text.rfind("\n", 0, m.start())
        return line_start + 1 if line_start >= 0 else m.start()
    return class_brace
