"""
Tests for the TypeScript / JavaScript symbol extractor.

Covers:
  - Exported and non-exported functions (with role classification)
  - Exported arrow functions
  - Exported classes (with methods, bases, implements, role)
  - Exported interfaces (with role classification)
  - Exported type aliases
  - Exported enums
  - Exported constants
  - Role classification heuristics:
    component, hook, api_endpoint, middleware, factory, entry_point,
    data_model, guard, pipe, exception, enum_type, test_helper, utility
  - Module path derivation
  - Language detection (typescript vs javascript)
  - File extension filtering
  - Edge cases (empty, binary, mixed files)
"""
from __future__ import annotations

import pytest

from analysis.evidence import SourceFile
from analysis.extractors.ts_symbols import TSSymbolExtractor


def _src(path: str, code: str) -> SourceFile:
    return SourceFile(path=path, content=code.encode(), language=None)


def _extract_all(code: str, path: str = "src/mod.ts") -> list:
    ext = TSSymbolExtractor()
    return list(ext.extract([_src(path, code)]))


def _by_name(items: list, name: str):
    for it in items:
        if it.extracted_value.get("symbol_name") == name:
            return it
    return None


# ---------------------------------------------------------------------------
# Exported functions
# ---------------------------------------------------------------------------

class TestFunctions:

    def test_exported_function_emits_symbol(self):
        items = _extract_all("export function greet(name: string): string {\n  return name;\n}\n")
        assert len(items) == 1
        v = items[0].extracted_value
        assert v["symbol_kind"] == "function"
        assert v["symbol_name"] == "greet"
        assert v["role"] == "utility"
        assert v["language"] == "typescript"
        assert items[0].evidence_type == "symbol"

    def test_async_function(self):
        items = _extract_all("export async function fetchData(): Promise<void> {\n}\n")
        assert len(items) == 1
        assert items[0].extracted_value["symbol_kind"] == "async_function"

    def test_non_exported_public_included(self):
        items = _extract_all("function helper() {\n}\n")
        assert len(items) == 1
        assert items[0].extracted_value["symbol_name"] == "helper"

    def test_underscore_non_exported_skipped(self):
        items = _extract_all("function _internal() {\n}\n")
        assert len(items) == 0

    def test_module_path(self):
        items = _extract_all("export function f() {\n}\n", path="src/utils/helpers.ts")
        assert items[0].extracted_value["module_path"] == "src.utils.helpers"
        assert items[0].extracted_value["qualified_name"] == "src.utils.helpers.f"

    def test_index_file_module_path(self):
        items = _extract_all("export function f() {\n}\n", path="src/utils/index.ts")
        assert items[0].extracted_value["module_path"] == "src.utils"


# ---------------------------------------------------------------------------
# Arrow functions
# ---------------------------------------------------------------------------

class TestArrows:

    def test_exported_arrow(self):
        items = _extract_all("export const add = (a: number, b: number) => {\n  return a + b;\n};\n")
        sym = _by_name(items, "add")
        assert sym is not None
        assert sym.extracted_value["symbol_kind"] == "function"

    def test_async_arrow(self):
        items = _extract_all("export const load = async () => {\n};\n")
        sym = _by_name(items, "load")
        assert sym is not None
        assert sym.extracted_value["symbol_kind"] == "async_function"

    def test_non_exported_underscore_arrow_skipped(self):
        items = _extract_all("const _internal = () => {\n};\n")
        assert _by_name(items, "_internal") is None


# ---------------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------------

class TestClasses:

    def test_exported_class(self):
        items = _extract_all(
            "export class UserService {\n"
            "  getUser(id: string): User {\n    return null;\n  }\n"
            "  deleteUser(id: string): void {\n  }\n"
            "}\n"
        )
        cls = _by_name(items, "UserService")
        assert cls is not None
        assert cls.extracted_value["symbol_kind"] == "class"
        assert "getUser" in cls.extracted_value["methods"]
        assert "deleteUser" in cls.extracted_value["methods"]

    def test_class_with_extends(self):
        items = _extract_all(
            "export class Admin extends Base {\n"
            "  promote(): void {\n  }\n"
            "}\n"
        )
        cls = _by_name(items, "Admin")
        assert "Base" in cls.extracted_value["bases"]

    def test_class_with_implements(self):
        items = _extract_all(
            "export class Logger implements ILogger {\n"
            "  log(msg: string): void {\n  }\n"
            "}\n"
        )
        cls = _by_name(items, "Logger")
        assert "ILogger" in cls.extracted_value["bases"]

    def test_class_methods_emitted_separately(self):
        items = _extract_all(
            "export class Svc {\n"
            "  run(): void {\n  }\n"
            "}\n"
        )
        names = {it.extracted_value["symbol_name"] for it in items}
        assert "Svc" in names
        assert "run" in names

    def test_constructor_not_emitted(self):
        items = _extract_all(
            "export class Config {\n"
            "  constructor(private path: string) {\n  }\n"
            "  load(): void {\n  }\n"
            "}\n"
        )
        names = {it.extracted_value["symbol_name"] for it in items}
        assert "constructor" not in names
        assert "load" in names


# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------

class TestInterfaces:

    def test_exported_interface(self):
        items = _extract_all(
            "export interface User {\n  id: string;\n  name: string;\n}\n"
        )
        iface = _by_name(items, "User")
        assert iface is not None
        assert iface.extracted_value["symbol_kind"] == "interface"

    def test_interface_with_extends(self):
        items = _extract_all(
            "export interface Admin extends User, Auditable {\n  role: string;\n}\n"
        )
        iface = _by_name(items, "Admin")
        assert "User" in iface.extracted_value["bases"]
        assert "Auditable" in iface.extracted_value["bases"]

    def test_non_exported_public_interface_included(self):
        items = _extract_all("interface Internal {\n  x: number;\n}\n")
        assert len(items) == 1

    def test_underscore_non_exported_interface_skipped(self):
        items = _extract_all("interface _Private {\n  x: number;\n}\n")
        assert len(items) == 0


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

class TestTypes:

    def test_exported_type(self):
        items = _extract_all("export type UserId = string;\n")
        sym = _by_name(items, "UserId")
        assert sym is not None
        assert sym.extracted_value["symbol_kind"] == "type_alias"

    def test_non_exported_type_included(self):
        items = _extract_all("type Internal = number;\n")
        assert len(items) == 1

    def test_underscore_non_exported_type_skipped(self):
        items = _extract_all("type _Hidden = boolean;\n")
        assert len(items) == 0


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TestEnums:

    def test_exported_enum(self):
        items = _extract_all(
            "export enum Status {\n  Active = 'active',\n  Inactive = 'inactive',\n}\n"
        )
        sym = _by_name(items, "Status")
        assert sym is not None
        assert sym.extracted_value["symbol_kind"] == "enum"
        assert sym.extracted_value["role"] == "enum_type"

    def test_const_enum(self):
        items = _extract_all(
            "export const enum Direction {\n  Up,\n  Down,\n}\n"
        )
        sym = _by_name(items, "Direction")
        assert sym is not None
        assert sym.extracted_value["symbol_kind"] == "enum"

    def test_non_exported_underscore_enum_skipped(self):
        items = _extract_all("enum _Private {\n  A,\n}\n")
        assert len(items) == 0


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:

    def test_exported_constant(self):
        items = _extract_all("export const MAX_RETRIES = 3;\n")
        sym = _by_name(items, "MAX_RETRIES")
        assert sym is not None
        assert sym.extracted_value["symbol_kind"] == "constant"
        assert sym.extracted_value["role"] == "utility"

    def test_non_exported_constant_skipped(self):
        items = _extract_all("const MAX_SIZE = 100;\n")
        assert _by_name(items, "MAX_SIZE") is None

    def test_lowercase_const_not_matched(self):
        items = _extract_all("export const myVar = 42;\n")
        assert _by_name(items, "myVar") is None


# ---------------------------------------------------------------------------
# Role classification
# ---------------------------------------------------------------------------

class TestRoleClassification:

    def test_react_hook(self):
        items = _extract_all("export function useAuth(): AuthState {\n}\n")
        assert items[0].extracted_value["role"] == "hook"

    def test_react_component_tsx(self):
        items = _extract_all(
            "export function UserCard(): JSX.Element {\n}\n",
            path="components/UserCard.tsx",
        )
        assert items[0].extracted_value["role"] == "component"

    def test_not_component_in_ts(self):
        items = _extract_all(
            "export function UserCard(): void {\n}\n",
            path="utils/UserCard.ts",
        )
        assert items[0].extracted_value["role"] != "component"

    def test_factory_function(self):
        items = _extract_all("export function createApp(): App {\n}\n")
        assert items[0].extracted_value["role"] == "factory"

    def test_middleware_function(self):
        items = _extract_all("export function authMiddleware(): void {\n}\n")
        assert items[0].extracted_value["role"] == "middleware"

    def test_entry_point_main(self):
        items = _extract_all("export function main(): void {\n}\n")
        assert items[0].extracted_value["role"] == "entry_point"

    def test_entry_point_bootstrap(self):
        items = _extract_all("export function bootstrap(): void {\n}\n")
        assert items[0].extracted_value["role"] == "entry_point"

    def test_test_helper(self):
        items = _extract_all(
            "export function mockUser(): User {\n}\n",
            path="src/__tests__/helpers.ts",
        )
        assert items[0].extracted_value["role"] == "test_helper"

    def test_class_exception(self):
        items = _extract_all(
            "export class AppError extends Error {\n"
            "  getMessage(): string {\n  }\n"
            "}\n"
        )
        cls = _by_name(items, "AppError")
        assert cls.extracted_value["role"] == "exception"

    def test_class_component(self):
        items = _extract_all(
            "export class Dashboard extends React.Component {\n"
            "  render(): JSX.Element {\n  }\n"
            "}\n"
        )
        cls = _by_name(items, "Dashboard")
        assert cls.extracted_value["role"] == "component"

    def test_class_guard(self):
        items = _extract_all(
            "export class AuthGuard implements CanActivate {\n"
            "  canActivate(): boolean {\n  }\n"
            "}\n"
        )
        cls = _by_name(items, "AuthGuard")
        assert cls.extracted_value["role"] == "guard"

    def test_class_middleware(self):
        items = _extract_all(
            "export class LoggerMiddleware {\n"
            "  use(): void {\n  }\n"
            "}\n"
        )
        cls = _by_name(items, "LoggerMiddleware")
        assert cls.extracted_value["role"] == "middleware"

    def test_interface_data_model(self):
        items = _extract_all(
            "export interface UserDto {\n  id: string;\n}\n"
        )
        iface = _by_name(items, "UserDto")
        assert iface.extracted_value["role"] == "data_model"

    def test_type_data_model(self):
        items = _extract_all("export type CreateUserDto = { name: string };\n")
        sym = _by_name(items, "CreateUserDto")
        assert sym.extracted_value["role"] == "data_model"


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

class TestLanguageDetection:

    def test_ts_language(self):
        items = _extract_all("export function f() {\n}\n", path="mod.ts")
        assert items[0].extracted_value["language"] == "typescript"

    def test_tsx_language(self):
        items = _extract_all("export function f() {\n}\n", path="mod.tsx")
        assert items[0].extracted_value["language"] == "typescript"

    def test_js_language(self):
        items = _extract_all("export function f() {\n}\n", path="mod.js")
        assert items[0].extracted_value["language"] == "javascript"

    def test_jsx_language(self):
        items = _extract_all("export function f() {\n}\n", path="mod.jsx")
        assert items[0].extracted_value["language"] == "javascript"

    def test_mts_language(self):
        items = _extract_all("export function f() {\n}\n", path="mod.mts")
        assert items[0].extracted_value["language"] == "typescript"

    def test_mjs_language(self):
        items = _extract_all("export function f() {\n}\n", path="mod.mjs")
        assert items[0].extracted_value["language"] == "javascript"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_empty_file(self):
        items = _extract_all("")
        assert len(items) == 0

    def test_binary_content(self):
        ext = TSSymbolExtractor()
        f = SourceFile(path="mod.ts", content=b"\x00\x01\xff", language=None)
        items = list(ext.extract([f]))
        assert len(items) == 0

    def test_py_file_skipped(self):
        items = _extract_all("export function nope() {\n}\n", path="app.py")
        assert len(items) == 0

    def test_evidence_type_is_symbol(self):
        items = _extract_all("export function f() {\n}\n")
        assert all(it.evidence_type == "symbol" for it in items)

    def test_locator_has_path(self):
        items = _extract_all("export function f() {\n}\n", path="src/x.ts")
        assert items[0].locator["path"] == "src/x.ts"

    def test_multiple_declarations(self):
        code = (
            "export function alpha() {\n}\n"
            "export class Beta {\n  run(): void {\n  }\n}\n"
            "export interface GammaDto {\n  id: string;\n}\n"
            "export type Delta = string;\n"
            "export enum Epsilon {\n  A,\n}\n"
            "export const MAX_SIZE = 10;\n"
        )
        items = _extract_all(code)
        names = {it.extracted_value["symbol_name"] for it in items}
        assert {"alpha", "Beta", "run", "GammaDto", "Delta", "Epsilon", "MAX_SIZE"} <= names

    def test_mixed_files(self):
        ext = TSSymbolExtractor()
        files = [
            _src("app.ts", "export function tsFunc() {\n}\n"),
            _src("app.py", "def pyFunc(): pass\n"),
            _src("lib.js", "export function jsFunc() {\n}\n"),
        ]
        items = list(ext.extract(files))
        names = {it.extracted_value["symbol_name"] for it in items}
        assert "tsFunc" in names
        assert "jsFunc" in names
        assert "pyFunc" not in names
