"""
Phase 3 — Symbol Intelligence tests.

Coverage:
  1. Symbol extractor (pure function):
     - Extracts public functions with signature and return type
     - Extracts async functions
     - Extracts classes with public methods and bases
     - Extracts class methods as separate symbols
     - Extracts module-level UPPER_CASE constants
     - Skips private (_-prefixed) symbols
     - Classifies roles: utility, data_model, entry_point, factory,
       api_endpoint, cli_command, exception, middleware
     - Extracts docstring summaries
     - Computes module_path from file path
  2. search_symbols query (DB-backed):
     - Finds symbols by name substring
     - Filters by symbol_kind
     - Filters by role
     - Filters by capability_id
     - Returns empty for no match
"""
from __future__ import annotations

import uuid

import pytest

from analysis.evidence import SourceFile
from analysis.extractors.symbols import SymbolExtractor, _path_to_module
from mcp_server import queries


# ---------------------------------------------------------------------------
# Symbol extractor — pure function tests
# ---------------------------------------------------------------------------

_SAMPLE_PY = b'''
"""Sample module docstring."""

MAX_RETRIES = 3

def fetch_data(url: str, timeout: int = 30) -> dict:
    """Fetch data from a URL."""
    pass

async def stream_events(source: str) -> None:
    """Stream events asynchronously."""
    pass

def _internal_helper():
    pass

def create_client(config: dict) -> object:
    """Factory for creating clients."""
    pass

class UserModel(BaseModel):
    """A user data model."""
    name: str
    email: str

    def validate_email(self) -> bool:
        pass

    def _private_method(self):
        pass

class RequestError(Exception):
    """Custom request error."""
    pass

class AuthMiddleware:
    """Authentication middleware."""
    def process(self, request):
        pass

def main():
    pass
'''

_FLASK_PY = b'''
from flask import Flask

app = Flask(__name__)

@app.route("/api/users")
def list_users():
    """List all users."""
    pass

@app.get("/api/users/<id>")
def get_user(id: str):
    pass
'''

_CLICK_PY = b'''
import click

@click.command()
def deploy(env: str):
    """Deploy to an environment."""
    pass
'''


class TestSymbolExtractor:

    def setup_method(self):
        self.extractor = SymbolExtractor()

    def _extract(self, content: bytes, path: str = "src/mymodule.py"):
        files = [SourceFile(path=path, content=content)]
        return list(self.extractor.extract(files))

    def test_extracts_public_function(self):
        items = self._extract(_SAMPLE_PY)
        fetch = [i for i in items if i.extracted_value.get("symbol_name") == "fetch_data"]
        assert len(fetch) == 1
        v = fetch[0].extracted_value
        assert v["symbol_kind"] == "function"
        assert v["signature"] == "url: str, timeout: int=30"
        assert v["return_type"] == "dict"

    def test_extracts_async_function(self):
        items = self._extract(_SAMPLE_PY)
        stream = [i for i in items if i.extracted_value.get("symbol_name") == "stream_events"]
        assert len(stream) == 1
        assert stream[0].extracted_value["symbol_kind"] == "async_function"

    def test_skips_private_functions(self):
        items = self._extract(_SAMPLE_PY)
        names = [i.extracted_value["symbol_name"] for i in items]
        assert "_internal_helper" not in names

    def test_extracts_class_with_methods(self):
        items = self._extract(_SAMPLE_PY)
        user_model = [i for i in items if i.extracted_value.get("symbol_name") == "UserModel"]
        assert len(user_model) == 1
        v = user_model[0].extracted_value
        assert v["symbol_kind"] == "class"
        assert "validate_email" in v["methods"]
        assert "_private_method" not in v.get("methods", [])

    def test_extracts_class_methods_separately(self):
        items = self._extract(_SAMPLE_PY)
        methods = [i for i in items if i.extracted_value.get("symbol_kind") == "method"]
        method_names = [i.extracted_value["symbol_name"] for i in methods]
        assert "validate_email" in method_names
        assert "process" in method_names
        assert "_private_method" not in method_names

    def test_extracts_constants(self):
        items = self._extract(_SAMPLE_PY)
        consts = [i for i in items if i.extracted_value.get("symbol_kind") == "constant"]
        assert len(consts) == 1
        assert consts[0].extracted_value["symbol_name"] == "MAX_RETRIES"

    def test_classifies_data_model(self):
        items = self._extract(_SAMPLE_PY)
        user_model = [i for i in items if i.extracted_value.get("symbol_name") == "UserModel"]
        assert user_model[0].extracted_value["role"] == "data_model"

    def test_classifies_exception(self):
        items = self._extract(_SAMPLE_PY)
        err = [i for i in items if i.extracted_value.get("symbol_name") == "RequestError"]
        assert err[0].extracted_value["role"] == "exception"

    def test_classifies_middleware(self):
        items = self._extract(_SAMPLE_PY)
        mw = [i for i in items if i.extracted_value.get("symbol_name") == "AuthMiddleware"]
        assert mw[0].extracted_value["role"] == "middleware"

    def test_classifies_factory(self):
        items = self._extract(_SAMPLE_PY)
        factory = [i for i in items if i.extracted_value.get("symbol_name") == "create_client"]
        assert factory[0].extracted_value["role"] == "factory"

    def test_classifies_entry_point(self):
        items = self._extract(_SAMPLE_PY)
        main_fn = [i for i in items if i.extracted_value.get("symbol_name") == "main"]
        assert main_fn[0].extracted_value["role"] == "entry_point"

    def test_classifies_api_endpoint(self):
        items = self._extract(_FLASK_PY, path="src/app.py")
        endpoints = [i for i in items if i.extracted_value.get("role") == "api_endpoint"]
        names = {i.extracted_value["symbol_name"] for i in endpoints}
        assert "list_users" in names
        assert "get_user" in names

    def test_classifies_cli_command(self):
        items = self._extract(_CLICK_PY, path="cli/deploy.py")
        deploy = [i for i in items if i.extracted_value.get("symbol_name") == "deploy"]
        assert deploy[0].extracted_value["role"] == "cli_command"

    def test_extracts_docstring_summary(self):
        items = self._extract(_SAMPLE_PY)
        fetch = [i for i in items if i.extracted_value.get("symbol_name") == "fetch_data"]
        assert fetch[0].extracted_value["docstring_summary"] == "Fetch data from a URL."

    def test_module_path_from_file_path(self):
        items = self._extract(_SAMPLE_PY, path="src/utils/helpers.py")
        fn = [i for i in items if i.extracted_value.get("symbol_name") == "fetch_data"]
        assert fn[0].extracted_value["module_path"] == "src.utils.helpers"

    def test_qualified_name(self):
        items = self._extract(_SAMPLE_PY, path="src/mymodule.py")
        fn = [i for i in items if i.extracted_value.get("symbol_name") == "fetch_data"]
        assert fn[0].extracted_value["qualified_name"] == "src.mymodule.fetch_data"

    def test_skips_non_python_files(self):
        items = list(self.extractor.extract([
            SourceFile(path="README.md", content=b"# Hello"),
        ]))
        assert items == []

    def test_skips_syntax_errors(self):
        items = list(self.extractor.extract([
            SourceFile(path="broken.py", content=b"def (broken:"),
        ]))
        assert items == []


class TestPathToModule:

    def test_simple_file(self):
        assert _path_to_module("src/utils.py") == "src.utils"

    def test_init_file(self):
        assert _path_to_module("src/core/__init__.py") == "src.core"

    def test_nested(self):
        assert _path_to_module("a/b/c/d.py") == "a.b.c.d"

    def test_windows_path(self):
        assert _path_to_module("src\\utils.py") == "src.utils"


# ---------------------------------------------------------------------------
# Seed helpers (DB tests)
# ---------------------------------------------------------------------------

def _mk_capability(conn, key: str, name: str) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability "
            "(normalized_key, display_name, ecosystem, kind) "
            "VALUES (%s, %s, 'pypi', 'library') RETURNING id",
            (key, name),
        )
        return cur.fetchone()[0]


def _mk_version(conn, capability_id: uuid.UUID) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability_version "
            "(capability_id, version_key, version_kind, display_version, lifecycle_state) "
            "VALUES (%s, %s, 'content-hash', '1.0.0', 'candidate') RETURNING id",
            (capability_id, f"content:{uuid.uuid4().hex[:8]}"),
        )
        return cur.fetchone()[0]


def _add_symbol(
    conn, version_id: uuid.UUID,
    symbol_name: str, qualified_name: str,
    symbol_kind: str = "function",
    role: str = "utility",
    signature: str | None = None,
    module_path: str = "mymodule",
    docstring_summary: str | None = None,
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability_symbol "
            "(capability_version_id, module_path, symbol_kind, symbol_name, "
            " qualified_name, signature, role, docstring_summary) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (version_id, module_path, symbol_kind, symbol_name,
             qualified_name, signature, role, docstring_summary),
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# DB-backed search_symbols tests
# ---------------------------------------------------------------------------

class TestSearchSymbols:

    def test_finds_by_name(self, conn):
        cap = _mk_capability(conn, "pypi:sym-lib-a", "sym-lib-a")
        ver = _mk_version(conn, cap)
        _add_symbol(conn, ver, "fetch_data", "mymodule.fetch_data",
                    signature="url: str")
        _add_symbol(conn, ver, "parse_response", "mymodule.parse_response")
        conn.commit()

        rows = queries.search_symbols(conn, query="fetch")
        assert len(rows) >= 1
        assert any(r["symbol_name"] == "fetch_data" for r in rows)

    def test_finds_by_qualified_name(self, conn):
        cap = _mk_capability(conn, "pypi:sym-lib-b", "sym-lib-b")
        ver = _mk_version(conn, cap)
        _add_symbol(conn, ver, "Client", "httpx.Client",
                    symbol_kind="class", module_path="httpx")
        conn.commit()

        rows = queries.search_symbols(conn, query="httpx.Client")
        assert len(rows) == 1
        assert rows[0]["symbol_name"] == "Client"
        assert rows[0]["normalized_key"] == "pypi:sym-lib-b"

    def test_filter_by_symbol_kind(self, conn):
        cap = _mk_capability(conn, "pypi:sym-lib-c", "sym-lib-c")
        ver = _mk_version(conn, cap)
        _add_symbol(conn, ver, "Widget", "widgets.Widget",
                    symbol_kind="class", module_path="widgets")
        _add_symbol(conn, ver, "create_widget", "widgets.create_widget",
                    symbol_kind="function", module_path="widgets")
        conn.commit()

        rows = queries.search_symbols(conn, query="widget", symbol_kind="class")
        names = [r["symbol_name"] for r in rows]
        assert "Widget" in names
        assert "create_widget" not in names

    def test_filter_by_role(self, conn):
        cap = _mk_capability(conn, "pypi:sym-lib-d", "sym-lib-d")
        ver = _mk_version(conn, cap)
        _add_symbol(conn, ver, "UserSchema", "models.UserSchema",
                    symbol_kind="class", role="data_model",
                    module_path="models")
        _add_symbol(conn, ver, "validate", "models.validate",
                    role="utility", module_path="models")
        conn.commit()

        rows = queries.search_symbols(conn, query="model", role="data_model")
        assert len(rows) >= 1
        assert all(r["role"] == "data_model" for r in rows)

    def test_filter_by_capability_id(self, conn):
        cap_a = _mk_capability(conn, "pypi:sym-lib-e", "sym-lib-e")
        cap_b = _mk_capability(conn, "pypi:sym-lib-f", "sym-lib-f")
        ver_a = _mk_version(conn, cap_a)
        ver_b = _mk_version(conn, cap_b)
        _add_symbol(conn, ver_a, "alpha", "a.alpha")
        _add_symbol(conn, ver_b, "alpha_beta", "b.alpha_beta")
        conn.commit()

        rows = queries.search_symbols(conn, query="alpha",
                                      capability_id=str(cap_a))
        assert all(r["capability_id"] == str(cap_a) for r in rows)

    def test_empty_query_returns_empty(self, conn):
        rows = queries.search_symbols(conn, query="")
        assert rows == []

    def test_no_match_returns_empty(self, conn):
        rows = queries.search_symbols(conn, query="zzz_nonexistent_symbol_zzz")
        assert rows == []

    def test_includes_display_version(self, conn):
        cap = _mk_capability(conn, "pypi:sym-lib-g", "sym-lib-g")
        ver = _mk_version(conn, cap)
        _add_symbol(conn, ver, "unique_sym_g", "g.unique_sym_g")
        conn.commit()

        rows = queries.search_symbols(conn, query="unique_sym_g")
        assert len(rows) == 1
        assert rows[0]["display_version"] == "1.0.0"

    def test_includes_docstring_summary(self, conn):
        cap = _mk_capability(conn, "pypi:sym-lib-h", "sym-lib-h")
        ver = _mk_version(conn, cap)
        _add_symbol(conn, ver, "documented_fn", "h.documented_fn",
                    docstring_summary="Does something useful.")
        conn.commit()

        rows = queries.search_symbols(conn, query="documented_fn")
        assert rows[0]["docstring_summary"] == "Does something useful."
