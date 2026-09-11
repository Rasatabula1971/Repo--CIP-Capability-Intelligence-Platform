"""
Tests for TS symbol ingestion pipeline.

Covers:
  1. Migration 027 — expanded CHECK constraints accept TS symbol kinds and roles
  2. ingest_symbols query — materializes evidence_item → capability_symbol
  3. search_symbols — can find ingested TS symbols by language filter
"""
from __future__ import annotations

import json
import uuid

import pytest

from mcp_server import queries


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _mk_provider(conn) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_provider (name, kind) "
            "VALUES (%s, 'code_host') "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "
            "RETURNING id",
            (f"test-provider-{uuid.uuid4().hex[:6]}",),
        )
        return cur.fetchone()[0]


def _mk_asset(conn, provider_id: uuid.UUID) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_asset (provider_id, external_key, display_name, kind) "
            "VALUES (%s, %s, 'test-asset', 'repository') RETURNING id",
            (provider_id, f"ext-{uuid.uuid4().hex[:8]}"),
        )
        return cur.fetchone()[0]


def _mk_revision(conn, asset_id: uuid.UUID) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_revision "
            "(source_asset_id, revision_key, snapshot_status, content_hash) "
            "VALUES (%s, %s, 'snapshotted', %s) RETURNING id",
            (asset_id, f"rev-{uuid.uuid4().hex[:8]}", f"hash-{uuid.uuid4().hex[:8]}"),
        )
        return cur.fetchone()[0]


def _mk_analysis_run(conn, revision_id: uuid.UUID) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_run "
            "(source_revision_id, extractor_config_version, status, started_at) "
            "VALUES (%s, 2, 'completed', now()) RETURNING id",
            (revision_id,),
        )
        return cur.fetchone()[0]


def _mk_capability(conn, key: str) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability "
            "(normalized_key, display_name, ecosystem, kind) "
            "VALUES (%s, %s, 'npm', 'library') RETURNING id",
            (key, key),
        )
        return cur.fetchone()[0]


def _mk_version(conn, cap_id: uuid.UUID) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability_version "
            "(capability_id, version_key, version_kind, display_version, lifecycle_state) "
            "VALUES (%s, %s, 'content-hash', '1.0.0', 'candidate') RETURNING id",
            (cap_id, f"content:{uuid.uuid4().hex[:8]}"),
        )
        return cur.fetchone()[0]


def _mk_symbol_evidence(
    conn, revision_id: uuid.UUID, run_id: uuid.UUID,
    symbol_name: str, qualified_name: str,
    symbol_kind: str = "function",
    role: str = "utility",
    language: str = "typescript",
    module_path: str = "src.utils",
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO evidence_item "
            "(source_revision_id, analysis_run_id, extractor_name, "
            " evidence_type, locator_kind, locator, extracted_value) "
            "VALUES (%s, %s, 'ts_symbols', 'symbol', 'file_range', "
            " %s::jsonb, %s::jsonb) RETURNING id",
            (
                revision_id, run_id,
                json.dumps({"path": "src/utils.ts", "start_line": 1, "end_line": 5}),
                json.dumps({
                    "module_path": module_path,
                    "symbol_kind": symbol_kind,
                    "symbol_name": symbol_name,
                    "qualified_name": qualified_name,
                    "role": role,
                    "language": language,
                }),
            ),
        )
        return cur.fetchone()[0]


def _setup_evidence(conn):
    prov = _mk_provider(conn)
    asset = _mk_asset(conn, prov)
    rev = _mk_revision(conn, asset)
    run = _mk_analysis_run(conn, rev)
    conn.commit()
    return rev, run


# ---------------------------------------------------------------------------
# Migration 027: expanded CHECK constraints
# ---------------------------------------------------------------------------

class TestExpandedConstraints:

    def test_ts_symbol_kinds_accepted(self, conn):
        cap = _mk_capability(conn, f"npm:ts-kinds-{uuid.uuid4().hex[:6]}")
        ver = _mk_version(conn, cap)
        conn.commit()
        for kind in ("interface", "type_alias", "enum"):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO capability_symbol "
                    "(capability_version_id, module_path, symbol_kind, "
                    " symbol_name, qualified_name, role, language) "
                    "VALUES (%s, 'mod', %s, %s, %s, 'utility', 'typescript')",
                    (ver, kind, f"Sym{kind}", f"mod.Sym{kind}"),
                )
            conn.commit()

    def test_ts_roles_accepted(self, conn):
        cap = _mk_capability(conn, f"npm:ts-roles-{uuid.uuid4().hex[:6]}")
        ver = _mk_version(conn, cap)
        conn.commit()
        for role in ("component", "hook", "guard", "pipe", "enum_type"):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO capability_symbol "
                    "(capability_version_id, module_path, symbol_kind, "
                    " symbol_name, qualified_name, role, language) "
                    "VALUES (%s, 'mod', 'function', %s, %s, %s, 'typescript')",
                    (ver, f"fn_{role}", f"mod.fn_{role}", role),
                )
            conn.commit()

    def test_invalid_kind_rejected(self, conn):
        cap = _mk_capability(conn, f"npm:ts-bad-kind-{uuid.uuid4().hex[:6]}")
        ver = _mk_version(conn, cap)
        conn.commit()
        import psycopg
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO capability_symbol "
                    "(capability_version_id, module_path, symbol_kind, "
                    " symbol_name, qualified_name, role, language) "
                    "VALUES (%s, 'mod', 'bogus', 'x', 'mod.x', 'utility', 'typescript')",
                    (ver,),
                )
        conn.rollback()

    def test_invalid_role_rejected(self, conn):
        cap = _mk_capability(conn, f"npm:ts-bad-role-{uuid.uuid4().hex[:6]}")
        ver = _mk_version(conn, cap)
        conn.commit()
        import psycopg
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO capability_symbol "
                    "(capability_version_id, module_path, symbol_kind, "
                    " symbol_name, qualified_name, role, language) "
                    "VALUES (%s, 'mod', 'function', 'x', 'mod.x', 'bogus', 'typescript')",
                    (ver,),
                )
        conn.rollback()


# ---------------------------------------------------------------------------
# ingest_symbols
# ---------------------------------------------------------------------------

class TestIngestSymbols:

    def test_ingests_symbol_evidence(self, conn):
        rev, run = _setup_evidence(conn)
        cap = _mk_capability(conn, f"npm:ingest-{uuid.uuid4().hex[:6]}")
        ver = _mk_version(conn, cap)
        conn.commit()
        _mk_symbol_evidence(conn, rev, run, "fetchData", "src.utils.fetchData")
        _mk_symbol_evidence(conn, rev, run, "parseJSON", "src.utils.parseJSON")
        conn.commit()

        result = queries.ingest_symbols(conn, capability_version_id=str(ver))
        assert result is not None
        assert result["inserted"] == 2
        assert result["skipped"] == 0
        assert result["total_evidence"] == 2

    def test_skips_duplicates(self, conn):
        rev, run = _setup_evidence(conn)
        cap = _mk_capability(conn, f"npm:dup-{uuid.uuid4().hex[:6]}")
        ver = _mk_version(conn, cap)
        conn.commit()
        _mk_symbol_evidence(conn, rev, run, "helper", "src.utils.helper")
        conn.commit()

        r1 = queries.ingest_symbols(conn, capability_version_id=str(ver))
        assert r1["inserted"] == 1

        _mk_symbol_evidence(conn, rev, run, "helper", "src.utils.helper")
        conn.commit()
        r2 = queries.ingest_symbols(conn, capability_version_id=str(ver))
        assert r2["skipped"] >= 1

    def test_scoped_by_revision(self, conn):
        rev1, run1 = _setup_evidence(conn)
        rev2, run2 = _setup_evidence(conn)
        cap = _mk_capability(conn, f"npm:scoped-{uuid.uuid4().hex[:6]}")
        ver = _mk_version(conn, cap)
        conn.commit()
        _mk_symbol_evidence(conn, rev1, run1, "fromRev1", "mod.fromRev1")
        _mk_symbol_evidence(conn, rev2, run2, "fromRev2", "mod.fromRev2")
        conn.commit()

        result = queries.ingest_symbols(
            conn, capability_version_id=str(ver),
            source_revision_id=str(rev1),
        )
        assert result["inserted"] == 1
        assert result["total_evidence"] == 1

    def test_returns_none_on_bad_id(self, conn):
        result = queries.ingest_symbols(conn, capability_version_id="bad")
        assert result is None

    def test_handles_ts_kinds_and_roles(self, conn):
        rev, run = _setup_evidence(conn)
        cap = _mk_capability(conn, f"npm:ts-full-{uuid.uuid4().hex[:6]}")
        ver = _mk_version(conn, cap)
        conn.commit()
        _mk_symbol_evidence(
            conn, rev, run, "UserDto", "src.models.UserDto",
            symbol_kind="interface", role="data_model",
        )
        _mk_symbol_evidence(
            conn, rev, run, "Status", "src.types.Status",
            symbol_kind="enum", role="enum_type",
        )
        _mk_symbol_evidence(
            conn, rev, run, "useAuth", "src.hooks.useAuth",
            symbol_kind="function", role="hook",
        )
        conn.commit()

        result = queries.ingest_symbols(conn, capability_version_id=str(ver))
        assert result["inserted"] == 3

    def test_empty_evidence(self, conn):
        cap = _mk_capability(conn, f"npm:empty-{uuid.uuid4().hex[:6]}")
        ver = _mk_version(conn, cap)
        conn.commit()

        result = queries.ingest_symbols(conn, capability_version_id=str(ver))
        assert result is not None
        assert result["inserted"] == 0
        assert result["total_evidence"] == 0


# ---------------------------------------------------------------------------
# search_symbols — TS language filter
# ---------------------------------------------------------------------------

class TestSearchSymbolsTS:

    def test_finds_ts_symbols(self, conn):
        cap = _mk_capability(conn, f"npm:search-ts-{uuid.uuid4().hex[:6]}")
        ver = _mk_version(conn, cap)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO capability_symbol "
                "(capability_version_id, module_path, symbol_kind, "
                " symbol_name, qualified_name, role, language) "
                "VALUES (%s, 'src.utils', 'function', 'fetchData', "
                " 'src.utils.fetchData', 'utility', 'typescript')",
                (ver,),
            )
        conn.commit()

        rows = queries.search_symbols(conn, query="fetchData", language="typescript")
        assert len(rows) >= 1
        assert any(r["symbol_name"] == "fetchData" for r in rows)
        assert all(r["language"] == "typescript" for r in rows if r["symbol_name"] == "fetchData")

    def test_language_filter_excludes_python(self, conn):
        cap = _mk_capability(conn, f"npm:filter-{uuid.uuid4().hex[:6]}")
        ver = _mk_version(conn, cap)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO capability_symbol "
                "(capability_version_id, module_path, symbol_kind, "
                " symbol_name, qualified_name, role, language) "
                "VALUES (%s, 'mod', 'function', 'compute', 'mod.compute', "
                " 'utility', 'python')",
                (ver,),
            )
        conn.commit()

        rows = queries.search_symbols(conn, query="compute", language="typescript")
        assert not any(r["symbol_name"] == "compute" for r in rows)

    def test_interface_kind_searchable(self, conn):
        cap = _mk_capability(conn, f"npm:iface-{uuid.uuid4().hex[:6]}")
        ver = _mk_version(conn, cap)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO capability_symbol "
                "(capability_version_id, module_path, symbol_kind, "
                " symbol_name, qualified_name, role, language) "
                "VALUES (%s, 'src.types', 'interface', 'UserProfile', "
                " 'src.types.UserProfile', 'data_model', 'typescript')",
                (ver,),
            )
        conn.commit()

        rows = queries.search_symbols(conn, query="UserProfile", symbol_kind="interface")
        assert len(rows) >= 1
        assert rows[0]["symbol_kind"] == "interface"
