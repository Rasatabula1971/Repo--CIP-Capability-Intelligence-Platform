"""
Phase 9 — Adapter Generation tests.

Coverage:
  1. classify_bridge (pure domain):
     - Subprocess hints and pairs
     - HTTP hints and pairs
     - MCP hints and pairs
     - pip_install pair
     - skill_invoke pair
     - type_transform (same runtime)
     - generic fallback
  2. generate_skeleton (pure domain):
     - All 7 bridge kinds produce AdapterSkeleton
     - Skeleton contains adapt function
     - Test code contains test function
     - io_transform flows into type_transform
  3. create_adapter_spec (DB):
     - Creates with auto-classified bridge kind
     - Returns None on bad source_id
     - Returns None on nonexistent capability
     - Returns None on duplicate pair
  4. get_adapter_spec (DB):
     - Returns existing spec
     - Returns None on missing pair
     - Returns None on bad id
  5. list_adapter_specs (DB):
     - Lists all specs
     - Filters by capability_id
     - Filters by status
     - Returns empty on no matches
  6. update_adapter_status (DB):
     - Advances status
     - Returns None on bad id
     - Returns None on invalid status
"""
from __future__ import annotations

import uuid

import pytest

from core.policy.adapter import (
    AdapterSkeleton,
    classify_bridge,
    generate_skeleton,
)
from mcp_server import queries


# ---------------------------------------------------------------------------
# classify_bridge — pure domain
# ---------------------------------------------------------------------------

class TestClassifyBridge:

    def test_subprocess_hint(self):
        assert classify_bridge("python_import", "unknown",
                               "subprocess.run() call") == "subprocess"

    def test_subprocess_pair(self):
        assert classify_bridge("python_import", "cli_subprocess") == "subprocess"

    def test_subprocess_reverse(self):
        assert classify_bridge("cli_subprocess", "python_import") == "subprocess"

    def test_http_hint(self):
        assert classify_bridge("unknown", "unknown",
                               "httpx.Client call") == "http"

    def test_http_fetch_hint(self):
        assert classify_bridge("unknown", "unknown",
                               "fetch() call") == "http"

    def test_http_pair(self):
        assert classify_bridge("python_import", "http_endpoint") == "http"

    def test_mcp_hint(self):
        assert classify_bridge("unknown", "unknown",
                               "mcp.ClientSession + stdio_client") == "mcp_client"

    def test_mcp_pair(self):
        assert classify_bridge("python_import", "mcp_stdio") == "mcp_client"

    def test_pip_install_pair(self):
        assert classify_bridge("git_clone", "python_import") == "pip_install"

    def test_skill_invoke_pair(self):
        assert classify_bridge("claude_skill", "python_import") == "skill_invoke"

    def test_type_transform_same_runtime(self):
        assert classify_bridge("python_import", "python_import") == "type_transform"

    def test_generic_fallback(self):
        assert classify_bridge("exotic_a", "exotic_b") == "generic"

    def test_git_clone_cli(self):
        assert classify_bridge("git_clone", "cli_subprocess") == "subprocess"


# ---------------------------------------------------------------------------
# generate_skeleton — pure domain
# ---------------------------------------------------------------------------

class TestGenerateSkeleton:

    @pytest.mark.parametrize("kind", [
        "subprocess", "http", "mcp_client", "type_transform",
        "pip_install", "skill_invoke", "generic",
    ])
    def test_produces_skeleton_for_all_kinds(self, kind):
        result = generate_skeleton(bridge_kind=kind)
        assert isinstance(result, AdapterSkeleton)
        assert result.bridge_kind == kind
        assert "def adapt" in result.adapter_code
        assert "def test_" in result.test_code

    def test_subprocess_skeleton_content(self):
        s = generate_skeleton("subprocess", source_name="mytool", target_name="consumer")
        assert "subprocess.run" in s.adapter_code
        assert "mytool" in s.adapter_code
        assert "consumer" in s.description

    def test_http_skeleton_content(self):
        s = generate_skeleton("http", source_name="api", target_name="client")
        assert "httpx" in s.adapter_code
        assert "api" in s.description

    def test_mcp_skeleton_content(self):
        s = generate_skeleton("mcp_client", source_name="server", target_name="app")
        assert "ClientSession" in s.adapter_code

    def test_type_transform_with_io(self):
        s = generate_skeleton(
            "type_transform",
            io_transform={"source_output": "DataFrame", "target_input": "dict"},
        )
        assert "DataFrame" in s.adapter_code
        assert "dict" in s.adapter_code

    def test_generic_raises_not_implemented(self):
        s = generate_skeleton("generic")
        assert "NotImplementedError" in s.adapter_code

    def test_skeleton_names_propagate(self):
        s = generate_skeleton(
            "http",
            source_name="weather_api",
            target_name="dashboard",
            source_runtime="http_endpoint",
            target_runtime="python_import",
        )
        assert s.source_runtime == "http_endpoint"
        assert s.target_runtime == "python_import"
        assert "weather_api" in s.adapter_code


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _mk_capability(conn, key: str, runtime: str = "python_import") -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability "
            "(normalized_key, display_name, ecosystem, kind, runtime) "
            "VALUES (%s, %s, 'pypi', 'library', %s) RETURNING id",
            (key, key, runtime),
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


# ---------------------------------------------------------------------------
# create_adapter_spec — DB
# ---------------------------------------------------------------------------

class TestCreateAdapterSpec:

    def test_creates_with_auto_bridge(self, conn):
        src = _mk_capability(conn, f"pypi:src-{uuid.uuid4().hex[:6]}", "python_import")
        tgt = _mk_capability(conn, f"pypi:tgt-{uuid.uuid4().hex[:6]}", "cli_subprocess")
        conn.commit()

        result = queries.create_adapter_spec(
            conn,
            source_id=str(src),
            target_id=str(tgt),
        )
        assert result is not None
        assert result["bridge_kind"] == "subprocess"
        assert result["status"] == "generated"
        assert "def adapt" in result["skeleton_code"]
        assert "def test_" in result["test_code"]

    def test_creates_http_bridge(self, conn):
        src = _mk_capability(conn, f"pypi:hsrc-{uuid.uuid4().hex[:6]}", "python_import")
        tgt = _mk_capability(conn, f"pypi:htgt-{uuid.uuid4().hex[:6]}", "http_endpoint")
        conn.commit()

        result = queries.create_adapter_spec(
            conn,
            source_id=str(src),
            target_id=str(tgt),
        )
        assert result is not None
        assert result["bridge_kind"] == "http"

    def test_returns_none_on_bad_id(self, conn):
        result = queries.create_adapter_spec(conn, source_id="bad", target_id="bad")
        assert result is None

    def test_returns_none_on_nonexistent(self, conn):
        result = queries.create_adapter_spec(
            conn,
            source_id=str(uuid.uuid4()),
            target_id=str(uuid.uuid4()),
        )
        assert result is None

    def test_returns_none_on_duplicate(self, conn):
        src = _mk_capability(conn, f"pypi:dup-s-{uuid.uuid4().hex[:6]}", "python_import")
        tgt = _mk_capability(conn, f"pypi:dup-t-{uuid.uuid4().hex[:6]}", "mcp_stdio")
        conn.commit()

        r1 = queries.create_adapter_spec(conn, source_id=str(src), target_id=str(tgt))
        assert r1 is not None
        r2 = queries.create_adapter_spec(conn, source_id=str(src), target_id=str(tgt))
        assert r2 is None

    def test_with_adapter_hint(self, conn):
        src = _mk_capability(conn, f"pypi:ah-s-{uuid.uuid4().hex[:6]}", "unknown_a")
        tgt = _mk_capability(conn, f"pypi:ah-t-{uuid.uuid4().hex[:6]}", "unknown_b")
        conn.commit()

        result = queries.create_adapter_spec(
            conn,
            source_id=str(src),
            target_id=str(tgt),
            adapter_hint="subprocess.run() call, capture stdout",
        )
        assert result is not None
        assert result["bridge_kind"] == "subprocess"


# ---------------------------------------------------------------------------
# get_adapter_spec — DB
# ---------------------------------------------------------------------------

class TestGetAdapterSpec:

    def test_returns_existing(self, conn):
        src = _mk_capability(conn, f"pypi:get-s-{uuid.uuid4().hex[:6]}", "python_import")
        tgt = _mk_capability(conn, f"pypi:get-t-{uuid.uuid4().hex[:6]}", "http_endpoint")
        conn.commit()

        queries.create_adapter_spec(conn, source_id=str(src), target_id=str(tgt))
        result = queries.get_adapter_spec(conn, source_id=str(src), target_id=str(tgt))
        assert result is not None
        assert result["bridge_kind"] == "http"
        assert result["skeleton_code"] is not None

    def test_returns_none_on_missing(self, conn):
        result = queries.get_adapter_spec(
            conn,
            source_id=str(uuid.uuid4()),
            target_id=str(uuid.uuid4()),
        )
        assert result is None

    def test_returns_none_on_bad_id(self, conn):
        result = queries.get_adapter_spec(conn, source_id="bad", target_id="bad")
        assert result is None


# ---------------------------------------------------------------------------
# list_adapter_specs — DB
# ---------------------------------------------------------------------------

class TestListAdapterSpecs:

    def test_lists_all(self, conn):
        s1 = _mk_capability(conn, f"pypi:la-s1-{uuid.uuid4().hex[:6]}", "python_import")
        t1 = _mk_capability(conn, f"pypi:la-t1-{uuid.uuid4().hex[:6]}", "cli_subprocess")
        s2 = _mk_capability(conn, f"pypi:la-s2-{uuid.uuid4().hex[:6]}", "python_import")
        t2 = _mk_capability(conn, f"pypi:la-t2-{uuid.uuid4().hex[:6]}", "http_endpoint")
        conn.commit()

        queries.create_adapter_spec(conn, source_id=str(s1), target_id=str(t1))
        queries.create_adapter_spec(conn, source_id=str(s2), target_id=str(t2))

        rows = queries.list_adapter_specs(conn)
        assert len(rows) == 2

    def test_filters_by_capability(self, conn):
        s1 = _mk_capability(conn, f"pypi:fc-s1-{uuid.uuid4().hex[:6]}", "python_import")
        t1 = _mk_capability(conn, f"pypi:fc-t1-{uuid.uuid4().hex[:6]}", "cli_subprocess")
        s2 = _mk_capability(conn, f"pypi:fc-s2-{uuid.uuid4().hex[:6]}", "python_import")
        t2 = _mk_capability(conn, f"pypi:fc-t2-{uuid.uuid4().hex[:6]}", "http_endpoint")
        conn.commit()

        queries.create_adapter_spec(conn, source_id=str(s1), target_id=str(t1))
        queries.create_adapter_spec(conn, source_id=str(s2), target_id=str(t2))

        rows = queries.list_adapter_specs(conn, capability_id=str(s1))
        assert len(rows) == 1
        assert rows[0]["source_id"] == str(s1)

    def test_filters_by_status(self, conn):
        s1 = _mk_capability(conn, f"pypi:fs-s1-{uuid.uuid4().hex[:6]}", "python_import")
        t1 = _mk_capability(conn, f"pypi:fs-t1-{uuid.uuid4().hex[:6]}", "mcp_stdio")
        conn.commit()

        queries.create_adapter_spec(conn, source_id=str(s1), target_id=str(t1))

        rows = queries.list_adapter_specs(conn, status="generated")
        assert len(rows) == 1

        rows = queries.list_adapter_specs(conn, status="tested")
        assert len(rows) == 0

    def test_empty_on_no_matches(self, conn):
        rows = queries.list_adapter_specs(conn, capability_id=str(uuid.uuid4()))
        assert rows == []


# ---------------------------------------------------------------------------
# update_adapter_status — DB
# ---------------------------------------------------------------------------

class TestUpdateAdapterStatus:

    def test_advances_status(self, conn):
        src = _mk_capability(conn, f"pypi:us-s-{uuid.uuid4().hex[:6]}", "python_import")
        tgt = _mk_capability(conn, f"pypi:us-t-{uuid.uuid4().hex[:6]}", "cli_subprocess")
        conn.commit()

        created = queries.create_adapter_spec(
            conn, source_id=str(src), target_id=str(tgt),
        )
        result = queries.update_adapter_status(
            conn, adapter_spec_id=str(created["id"]), status="reviewed",
        )
        assert result is not None
        assert result["status"] == "reviewed"

    def test_returns_none_on_bad_id(self, conn):
        result = queries.update_adapter_status(
            conn, adapter_spec_id="bad", status="reviewed",
        )
        assert result is None

    def test_returns_none_on_nonexistent(self, conn):
        result = queries.update_adapter_status(
            conn,
            adapter_spec_id=str(uuid.uuid4()),
            status="reviewed",
        )
        assert result is None

    def test_returns_none_on_invalid_status(self, conn):
        src = _mk_capability(conn, f"pypi:is-s-{uuid.uuid4().hex[:6]}", "python_import")
        tgt = _mk_capability(conn, f"pypi:is-t-{uuid.uuid4().hex[:6]}", "cli_subprocess")
        conn.commit()

        created = queries.create_adapter_spec(
            conn, source_id=str(src), target_id=str(tgt),
        )
        result = queries.update_adapter_status(
            conn, adapter_spec_id=str(created["id"]), status="bogus",
        )
        assert result is None
