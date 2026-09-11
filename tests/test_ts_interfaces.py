"""
Tests for the TypeScript / JavaScript interface extractor.

Covers:
  - Exported functions (regular, async, with generics, with return types)
  - Exported arrow functions (const, async)
  - Exported classes (with extends, implements, methods)
  - Exported interfaces (with extends, members)
  - Exported type aliases
  - Non-exported declarations are skipped
  - File extension filtering (.ts, .tsx, .js, .jsx, .mjs, .mts)
  - Language detection (typescript vs javascript)
  - Malformed / empty files handled gracefully
"""
from __future__ import annotations

import pytest

from analysis.evidence import SourceFile
from analysis.extractors.ts_interfaces import TSInterfaceExtractor


def _src(path: str, code: str) -> SourceFile:
    return SourceFile(path=path, content=code.encode(), language=None)


def _extract_all(code: str, path: str = "mod.ts") -> list:
    ext = TSInterfaceExtractor()
    return list(ext.extract([_src(path, code)]))


# ---------------------------------------------------------------------------
# Exported functions
# ---------------------------------------------------------------------------

class TestExportedFunctions:

    def test_simple_exported_function(self):
        items = _extract_all(
            "export function greet(name: string): string {\n"
            "  return `Hello ${name}`;\n"
            "}\n"
        )
        assert len(items) == 1
        v = items[0].extracted_value
        assert v["kind"] == "function"
        assert v["name"] == "greet"
        assert "name: string" in v["signature"]
        assert v["returns"] == "string"
        assert v["language"] == "typescript"

    def test_async_exported_function(self):
        items = _extract_all(
            "export async function fetchData(url: string): Promise<Response> {\n"
            "  return fetch(url);\n"
            "}\n"
        )
        assert len(items) == 1
        v = items[0].extracted_value
        assert v["kind"] == "async_function"
        assert v["name"] == "fetchData"

    def test_export_default_function(self):
        items = _extract_all(
            "export default function main() {\n"
            "  console.log('hello');\n"
            "}\n"
        )
        assert len(items) == 1
        assert items[0].extracted_value["name"] == "main"

    def test_non_exported_function_skipped(self):
        items = _extract_all(
            "function helper(x: number): number {\n"
            "  return x + 1;\n"
            "}\n"
        )
        assert len(items) == 0

    def test_function_with_generics(self):
        items = _extract_all(
            "export function identity<T>(value: T): T {\n"
            "  return value;\n"
            "}\n"
        )
        assert len(items) == 1
        assert items[0].extracted_value["name"] == "identity"

    def test_function_line_numbers(self):
        items = _extract_all(
            "\n\nexport function foo() {\n"
            "  return 1;\n"
            "}\n"
        )
        assert len(items) == 1
        loc = items[0].locator
        assert loc["start_line"] == 3
        assert loc["end_line"] == 5


# ---------------------------------------------------------------------------
# Exported arrow functions
# ---------------------------------------------------------------------------

class TestExportedArrows:

    def test_const_arrow(self):
        items = _extract_all(
            "export const add = (a: number, b: number): number => {\n"
            "  return a + b;\n"
            "};\n"
        )
        assert len(items) == 1
        v = items[0].extracted_value
        assert v["kind"] == "function"
        assert v["name"] == "add"

    def test_async_arrow(self):
        items = _extract_all(
            "export const fetchUser = async (id: string) => {\n"
            "  return await db.find(id);\n"
            "};\n"
        )
        assert len(items) == 1
        assert items[0].extracted_value["kind"] == "async_function"
        assert items[0].extracted_value["name"] == "fetchUser"

    def test_default_arrow(self):
        items = _extract_all(
            "export default const handler = (req: Request) => {\n"
            "  return new Response();\n"
            "};\n"
        )
        assert len(items) == 1
        assert items[0].extracted_value["name"] == "handler"


# ---------------------------------------------------------------------------
# Exported classes
# ---------------------------------------------------------------------------

class TestExportedClasses:

    def test_simple_class(self):
        items = _extract_all(
            "export class UserService {\n"
            "  getUser(id: string): User {\n"
            "    return this.db.find(id);\n"
            "  }\n"
            "  deleteUser(id: string): void {\n"
            "    this.db.remove(id);\n"
            "  }\n"
            "}\n"
        )
        assert len(items) == 1
        v = items[0].extracted_value
        assert v["kind"] == "class"
        assert v["name"] == "UserService"
        assert "getUser" in v["public_methods"]
        assert "deleteUser" in v["public_methods"]
        assert v["language"] == "typescript"

    def test_class_with_extends(self):
        items = _extract_all(
            "export class AdminService extends UserService {\n"
            "  promote(id: string): void {}\n"
            "}\n"
        )
        assert len(items) == 1
        v = items[0].extracted_value
        assert "UserService" in v["bases"]

    def test_class_with_implements(self):
        items = _extract_all(
            "export class Logger implements ILogger, Serializable {\n"
            "  log(msg: string): void {}\n"
            "}\n"
        )
        assert len(items) == 1
        v = items[0].extracted_value
        assert "ILogger" in v["bases"]
        assert "Serializable" in v["bases"]

    def test_class_constructor_not_in_methods(self):
        items = _extract_all(
            "export class Config {\n"
            "  constructor(private readonly path: string) {}\n"
            "  load(): void {}\n"
            "}\n"
        )
        assert len(items) == 1
        methods = items[0].extracted_value["public_methods"]
        assert "constructor" not in methods
        assert "load" in methods

    def test_non_exported_class_skipped(self):
        items = _extract_all(
            "class InternalHelper {\n"
            "  run(): void {}\n"
            "}\n"
        )
        assert len(items) == 0

    def test_abstract_class(self):
        items = _extract_all(
            "export abstract class BaseHandler {\n"
            "  abstract handle(req: Request): Response;\n"
            "  log(msg: string): void {}\n"
            "}\n"
        )
        assert len(items) == 1
        assert items[0].extracted_value["name"] == "BaseHandler"


# ---------------------------------------------------------------------------
# Exported interfaces
# ---------------------------------------------------------------------------

class TestExportedInterfaces:

    def test_simple_interface(self):
        items = _extract_all(
            "export interface User {\n"
            "  id: string;\n"
            "  name: string;\n"
            "  email: string;\n"
            "}\n"
        )
        assert len(items) == 1
        v = items[0].extracted_value
        assert v["kind"] == "interface"
        assert v["name"] == "User"
        assert "id" in v["members"]
        assert "name" in v["members"]
        assert "email" in v["members"]

    def test_interface_with_extends(self):
        items = _extract_all(
            "export interface Admin extends User, Auditable {\n"
            "  role: string;\n"
            "}\n"
        )
        assert len(items) == 1
        v = items[0].extracted_value
        assert "User" in v["bases"]
        assert "Auditable" in v["bases"]

    def test_interface_with_methods(self):
        items = _extract_all(
            "export interface Repository {\n"
            "  find(id: string): Promise<Entity>;\n"
            "  save(entity: Entity): Promise<void>;\n"
            "}\n"
        )
        assert len(items) == 1
        v = items[0].extracted_value
        assert "find" in v["members"]
        assert "save" in v["members"]

    def test_interface_optional_members(self):
        items = _extract_all(
            "export interface Config {\n"
            "  host: string;\n"
            "  port?: number;\n"
            "}\n"
        )
        assert len(items) == 1
        assert "port" in items[0].extracted_value["members"]

    def test_non_exported_interface_skipped(self):
        items = _extract_all(
            "interface Internal {\n"
            "  x: number;\n"
            "}\n"
        )
        assert len(items) == 0


# ---------------------------------------------------------------------------
# Exported type aliases
# ---------------------------------------------------------------------------

class TestExportedTypes:

    def test_simple_type(self):
        items = _extract_all(
            "export type UserId = string;\n"
        )
        assert len(items) == 1
        v = items[0].extracted_value
        assert v["kind"] == "type_alias"
        assert v["name"] == "UserId"
        assert v["definition"] == "string"

    def test_union_type(self):
        items = _extract_all(
            "export type Status = 'active' | 'inactive' | 'pending';\n"
        )
        assert len(items) == 1
        assert items[0].extracted_value["name"] == "Status"

    def test_generic_type(self):
        items = _extract_all(
            "export type Result<T> = { ok: true; data: T } | { ok: false; error: string };\n"
        )
        assert len(items) == 1
        assert items[0].extracted_value["name"] == "Result"

    def test_non_exported_type_skipped(self):
        items = _extract_all(
            "type Internal = number;\n"
        )
        assert len(items) == 0


# ---------------------------------------------------------------------------
# File extension filtering
# ---------------------------------------------------------------------------

class TestFileExtensions:

    def test_ts_accepted(self):
        items = _extract_all("export function f() {}\n", path="app.ts")
        assert len(items) == 1
        assert items[0].extracted_value["language"] == "typescript"

    def test_tsx_accepted(self):
        items = _extract_all("export function Component() {}\n", path="App.tsx")
        assert len(items) == 1
        assert items[0].extracted_value["language"] == "typescript"

    def test_js_accepted(self):
        items = _extract_all("export function run() {}\n", path="index.js")
        assert len(items) == 1
        assert items[0].extracted_value["language"] == "javascript"

    def test_jsx_accepted(self):
        items = _extract_all("export function Widget() {}\n", path="widget.jsx")
        assert len(items) == 1
        assert items[0].extracted_value["language"] == "javascript"

    def test_mjs_accepted(self):
        items = _extract_all("export function esm() {}\n", path="lib.mjs")
        assert len(items) == 1
        assert items[0].extracted_value["language"] == "javascript"

    def test_mts_accepted(self):
        items = _extract_all("export function mod() {}\n", path="lib.mts")
        assert len(items) == 1
        assert items[0].extracted_value["language"] == "typescript"

    def test_py_rejected(self):
        items = _extract_all("export function nope() {}\n", path="script.py")
        assert len(items) == 0

    def test_json_rejected(self):
        items = _extract_all("{}\n", path="package.json")
        assert len(items) == 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_empty_file(self):
        items = _extract_all("")
        assert len(items) == 0

    def test_comments_only(self):
        items = _extract_all("// just a comment\n/* block comment */\n")
        assert len(items) == 0

    def test_binary_content(self):
        ext = TSInterfaceExtractor()
        f = SourceFile(path="mod.ts", content=b"\x00\x01\x02\xff", language=None)
        items = list(ext.extract([f]))
        assert len(items) == 0

    def test_multiple_exports(self):
        code = (
            "export function alpha() {}\n"
            "export function beta() {}\n"
            "function internal() {}\n"
            "export class Gamma {}\n"
            "export interface Delta {\n  x: number;\n}\n"
            "export type Epsilon = string;\n"
        )
        items = _extract_all(code)
        names = {it.extracted_value["name"] for it in items}
        assert names == {"alpha", "beta", "Gamma", "Delta", "Epsilon"}

    def test_mixed_ts_and_py_files(self):
        ext = TSInterfaceExtractor()
        files = [
            _src("app.ts", "export function tsFunc() {}\n"),
            _src("app.py", "def pyFunc(): pass\n"),
            _src("util.js", "export function jsFunc() {}\n"),
        ]
        items = list(ext.extract(files))
        names = {it.extracted_value["name"] for it in items}
        assert names == {"tsFunc", "jsFunc"}

    def test_evidence_type_is_interface(self):
        items = _extract_all("export function f() {}\n")
        assert items[0].evidence_type == "interface"

    def test_locator_kind_is_file_range(self):
        items = _extract_all("export function f() {}\n")
        assert items[0].locator_kind == "file_range"
        assert items[0].locator["path"] == "mod.ts"
