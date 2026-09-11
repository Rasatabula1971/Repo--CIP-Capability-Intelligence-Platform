"""
Symbol extractor (Phase 3).

Enhanced Python AST parser that emits granular symbol evidence:
functions, async functions, classes (with public methods), constants,
and module-level __all__ exports. Classifies each symbol by role
based on decorators, base classes, and naming conventions.

Role classification heuristics:
  entry_point    — decorated with @app.route, @router, main() convention
  cli_command    — decorated with @click.command, @app.command, or similar
  data_model     — subclasses BaseModel, is @dataclass, NamedTuple
  api_endpoint   — decorated with @app.get/post/put/delete/patch
  decorator      — function that returns a wrapper (heuristic)
  factory        — function named create_*, make_*, build_*
  middleware     — decorated with @middleware or named *_middleware
  exception      — subclasses Exception/Error
  test_helper    — in test files, named assert_* or mock_*
  utility        — default / everything else

Scoped to Python. Node/TypeScript deferred to a later phase.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from analysis.evidence import (
    LOCATOR_FILE_RANGE,
    EvidenceItem,
    SourceFile,
)


@dataclass(frozen=True)
class SymbolInfo:
    module_path: str
    symbol_kind: str
    symbol_name: str
    qualified_name: str
    signature: str | None = None
    return_type: str | None = None
    docstring_summary: str | None = None
    role: str = "utility"
    language: str = "python"
    methods: list[str] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)


class SymbolExtractor:
    name = "symbols"

    def extract(self, files: Iterable[SourceFile]) -> Iterator[EvidenceItem]:
        for f in files:
            if not f.path.endswith(".py"):
                continue
            try:
                tree = ast.parse(f.content, filename=f.path)
            except SyntaxError:
                continue
            module_path = _path_to_module(f.path)
            yield from self._walk_module(tree, f, module_path)

    def _walk_module(
        self, tree: ast.Module, f: SourceFile, module_path: str,
    ) -> Iterator[EvidenceItem]:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_") and node.name != "__init__":
                    continue
                info = self._extract_function(node, module_path, f)
                yield self._to_evidence(info, node, f)

            elif isinstance(node, ast.ClassDef):
                if node.name.startswith("_"):
                    continue
                info = self._extract_class(node, module_path, f)
                yield self._to_evidence(info, node, f)
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if child.name.startswith("_"):
                            continue
                        method_info = self._extract_method(
                            child, module_path, node.name, f,
                        )
                        yield self._to_evidence(method_info, child, f)

            elif isinstance(node, ast.Assign):
                sym = self._try_constant(node, module_path)
                if sym is not None:
                    yield self._to_evidence(sym, node, f)

    def _extract_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef,
        module_path: str, f: SourceFile,
    ) -> SymbolInfo:
        kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
        role = _classify_function_role(node, f.path)
        return SymbolInfo(
            module_path=module_path,
            symbol_kind=kind,
            symbol_name=node.name,
            qualified_name=f"{module_path}.{node.name}" if module_path else node.name,
            signature=_format_signature(node.args),
            return_type=_format_annotation(node.returns),
            docstring_summary=_extract_docstring(node),
            role=role,
        )

    def _extract_class(
        self, node: ast.ClassDef, module_path: str, f: SourceFile,
    ) -> SymbolInfo:
        bases = [_format_annotation(b) for b in node.bases]
        methods = sorted(
            child.name
            for child in node.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not child.name.startswith("_")
        )
        role = _classify_class_role(node, bases)
        return SymbolInfo(
            module_path=module_path,
            symbol_kind="class",
            symbol_name=node.name,
            qualified_name=f"{module_path}.{node.name}" if module_path else node.name,
            docstring_summary=_extract_docstring(node),
            role=role,
            methods=methods,
            bases=bases,
        )

    def _extract_method(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef,
        module_path: str, class_name: str, f: SourceFile,
    ) -> SymbolInfo:
        kind = "async_method" if isinstance(node, ast.AsyncFunctionDef) else "method"
        qname_base = f"{module_path}.{class_name}" if module_path else class_name
        return SymbolInfo(
            module_path=module_path,
            symbol_kind=kind,
            symbol_name=node.name,
            qualified_name=f"{qname_base}.{node.name}",
            signature=_format_signature(node.args),
            return_type=_format_annotation(node.returns),
            docstring_summary=_extract_docstring(node),
            role=_classify_method_role(node),
        )

    def _try_constant(
        self, node: ast.Assign, module_path: str,
    ) -> SymbolInfo | None:
        if len(node.targets) != 1:
            return None
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            return None
        name = target.id
        if not name.isupper() or name.startswith("_"):
            return None
        return SymbolInfo(
            module_path=module_path,
            symbol_kind="constant",
            symbol_name=name,
            qualified_name=f"{module_path}.{name}" if module_path else name,
            role="utility",
        )

    def _to_evidence(
        self, info: SymbolInfo, node: ast.AST, f: SourceFile,
    ) -> EvidenceItem:
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
        if info.docstring_summary:
            value["docstring_summary"] = info.docstring_summary
        if info.methods:
            value["methods"] = info.methods
        if info.bases:
            value["bases"] = info.bases
        return EvidenceItem(
            evidence_type="symbol",
            locator_kind=LOCATOR_FILE_RANGE,
            locator={
                "path": f.path,
                "start_line": getattr(node, "lineno", 1),
                "end_line": getattr(node, "end_lineno", None)
                            or getattr(node, "lineno", 1),
            },
            extracted_value=value,
        )


# ---------------------------------------------------------------------------
# Role classification
# ---------------------------------------------------------------------------

_ENTRY_POINT_DECORATORS = {
    "route", "get", "post", "put", "delete", "patch",
    "api_route", "websocket",
}

_CLI_DECORATORS = {
    "command", "group", "cli",
}

_DATA_MODEL_BASES = {
    "BaseModel", "Model", "Schema",
    "NamedTuple", "TypedDict",
}

_EXCEPTION_BASES = {"Exception", "Error", "BaseException"}


def _get_decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> set[str]:
    names: set[str] = set()
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name):
            names.add(dec.id)
        elif isinstance(dec, ast.Attribute):
            names.add(dec.attr)
        elif isinstance(dec, ast.Call):
            if isinstance(dec.func, ast.Name):
                names.add(dec.func.id)
            elif isinstance(dec.func, ast.Attribute):
                names.add(dec.func.attr)
    return names


def _classify_function_role(
    node: ast.FunctionDef | ast.AsyncFunctionDef, file_path: str,
) -> str:
    dec_names = _get_decorator_names(node)

    if dec_names & _ENTRY_POINT_DECORATORS:
        return "api_endpoint"
    if dec_names & _CLI_DECORATORS:
        return "cli_command"
    if "middleware" in dec_names or node.name.endswith("_middleware"):
        return "middleware"

    if node.name in ("main", "cli", "app"):
        return "entry_point"
    if node.name.startswith(("create_", "make_", "build_")):
        return "factory"
    if "test" in file_path and node.name.startswith(("assert_", "mock_")):
        return "test_helper"

    return "utility"


def _classify_class_role(node: ast.ClassDef, bases: list[str]) -> str:
    dec_names = _get_decorator_names(node)

    if "dataclass" in dec_names:
        return "data_model"

    base_names = {b.split(".")[-1] for b in bases if b}
    if base_names & _DATA_MODEL_BASES:
        return "data_model"
    if base_names & _EXCEPTION_BASES:
        return "exception"

    if node.name.endswith("Middleware"):
        return "middleware"

    return "utility"


def _classify_method_role(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    dec_names = _get_decorator_names(node)
    if dec_names & _ENTRY_POINT_DECORATORS:
        return "api_endpoint"
    if dec_names & _CLI_DECORATORS:
        return "cli_command"
    return "utility"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _path_to_module(path: str) -> str:
    p = path.replace("\\", "/")
    if p.endswith("/__init__.py"):
        p = p[:-len("/__init__.py")]
    elif p.endswith(".py"):
        p = p[:-3]
    return p.replace("/", ".")


def _extract_docstring(node: ast.AST) -> str | None:
    if not hasattr(node, "body") or not node.body:
        return None
    first = node.body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
        val = first.value.value
        if isinstance(val, str):
            lines = val.strip().splitlines()
            return lines[0].strip() if lines else None
    return None


def _format_signature(args: ast.arguments) -> str:
    parts: list[str] = []
    pos_defaults_offset = len(args.args) - len(args.defaults)

    for i, arg in enumerate(args.args):
        if arg.arg == "self" or arg.arg == "cls":
            continue
        s = arg.arg
        if arg.annotation is not None:
            s += f": {_format_annotation(arg.annotation)}"
        default_i = i - pos_defaults_offset
        if default_i >= 0:
            s += f"={_format_default(args.defaults[default_i])}"
        parts.append(s)

    if args.vararg:
        parts.append(f"*{args.vararg.arg}")

    for i, arg in enumerate(args.kwonlyargs):
        s = arg.arg
        if arg.annotation is not None:
            s += f": {_format_annotation(arg.annotation)}"
        default = args.kw_defaults[i] if i < len(args.kw_defaults) else None
        if default is not None:
            s += f"={_format_default(default)}"
        parts.append(s)

    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")

    return ", ".join(parts)


def _format_annotation(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _format_default(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "..."
