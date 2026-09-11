"""
Phase 8 — Inspection Run Tracking tests.

Coverage:
  1. record_inspection_run:
     - Creates a running inspection record
     - Returns None on bad capability_version_id
     - Returns None on nonexistent version
  2. finish_inspection_run:
     - Marks run as completed with evidence count
     - Marks run as failed with error detail
     - Returns None on bad run_id
     - Cannot finish an already-finished run
     - Rejects invalid status
  3. inspection_history:
     - Returns runs for head version
     - Filters by extractor_name
     - Shows extractors_covered and extractors_missing
     - Returns empty runs when no inspections exist
     - Returns None on bad capability_id
     - Orders by started_at DESC
"""
from __future__ import annotations

import uuid

import pytest

from mcp_server import queries


# ---------------------------------------------------------------------------
# Seed helpers
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


# ---------------------------------------------------------------------------
# record_inspection_run
# ---------------------------------------------------------------------------

class TestRecordInspectionRun:

    def test_creates_running_record(self, conn):
        cap = _mk_capability(conn, f"pypi:lib-{uuid.uuid4().hex[:6]}", "lib")
        cv = _mk_version(conn, cap)
        conn.commit()

        result = queries.record_inspection_run(
            conn,
            capability_version_id=str(cv),
            extractor_name="manifests",
            extractor_version="2.0.0",
            source_revision="abc123",
        )
        assert result is not None
        assert result["status"] == "running"
        assert result["extractor_name"] == "manifests"
        assert result["extractor_version"] == "2.0.0"
        assert result["source_revision"] == "abc123"
        assert "id" in result

    def test_returns_none_on_bad_id(self, conn):
        result = queries.record_inspection_run(
            conn, capability_version_id="bad", extractor_name="manifests",
        )
        assert result is None

    def test_returns_none_on_nonexistent_version(self, conn):
        result = queries.record_inspection_run(
            conn,
            capability_version_id=str(uuid.uuid4()),
            extractor_name="manifests",
        )
        assert result is None

    def test_accepts_all_extractor_names(self, conn):
        cap = _mk_capability(conn, f"pypi:ext-{uuid.uuid4().hex[:6]}", "ext")
        cv = _mk_version(conn, cap)
        conn.commit()

        for name in ["manifests", "licenses", "interfaces", "symbols", "secrets", "tests"]:
            result = queries.record_inspection_run(
                conn, capability_version_id=str(cv), extractor_name=name,
            )
            assert result is not None, f"failed for extractor {name}"
            assert result["extractor_name"] == name


# ---------------------------------------------------------------------------
# finish_inspection_run
# ---------------------------------------------------------------------------

class TestFinishInspectionRun:

    def test_completes_with_evidence_count(self, conn):
        cap = _mk_capability(conn, f"pypi:fin-{uuid.uuid4().hex[:6]}", "fin")
        cv = _mk_version(conn, cap)
        conn.commit()

        started = queries.record_inspection_run(
            conn, capability_version_id=str(cv), extractor_name="licenses",
        )
        result = queries.finish_inspection_run(
            conn, run_id=started["id"], status="completed", evidence_count=42,
        )
        assert result is not None
        assert result["status"] == "completed"
        assert result["evidence_count"] == 42
        assert result["finished_at"] is not None

    def test_marks_as_failed(self, conn):
        cap = _mk_capability(conn, f"pypi:fail-{uuid.uuid4().hex[:6]}", "fail")
        cv = _mk_version(conn, cap)
        conn.commit()

        started = queries.record_inspection_run(
            conn, capability_version_id=str(cv), extractor_name="interfaces",
        )
        result = queries.finish_inspection_run(
            conn, run_id=started["id"], status="failed",
            error_detail="SyntaxError in foo.py",
        )
        assert result is not None
        assert result["status"] == "failed"

    def test_returns_none_on_bad_id(self, conn):
        result = queries.finish_inspection_run(
            conn, run_id="bad", status="completed",
        )
        assert result is None

    def test_cannot_finish_twice(self, conn):
        cap = _mk_capability(conn, f"pypi:twice-{uuid.uuid4().hex[:6]}", "twice")
        cv = _mk_version(conn, cap)
        conn.commit()

        started = queries.record_inspection_run(
            conn, capability_version_id=str(cv), extractor_name="secrets",
        )
        queries.finish_inspection_run(
            conn, run_id=started["id"], status="completed",
        )
        result = queries.finish_inspection_run(
            conn, run_id=started["id"], status="completed",
        )
        assert result is None

    def test_rejects_invalid_status(self, conn):
        cap = _mk_capability(conn, f"pypi:inv-{uuid.uuid4().hex[:6]}", "inv")
        cv = _mk_version(conn, cap)
        conn.commit()

        started = queries.record_inspection_run(
            conn, capability_version_id=str(cv), extractor_name="tests",
        )
        result = queries.finish_inspection_run(
            conn, run_id=started["id"], status="running",
        )
        assert result is None


# ---------------------------------------------------------------------------
# inspection_history
# ---------------------------------------------------------------------------

class TestInspectionHistory:

    def test_returns_runs(self, conn):
        cap = _mk_capability(conn, f"pypi:hist-{uuid.uuid4().hex[:6]}", "hist")
        cv = _mk_version(conn, cap)
        conn.commit()

        r1 = queries.record_inspection_run(
            conn, capability_version_id=str(cv), extractor_name="manifests",
        )
        queries.finish_inspection_run(
            conn, run_id=r1["id"], status="completed", evidence_count=5,
        )
        r2 = queries.record_inspection_run(
            conn, capability_version_id=str(cv), extractor_name="licenses",
        )
        queries.finish_inspection_run(
            conn, run_id=r2["id"], status="completed", evidence_count=1,
        )

        result = queries.inspection_history(conn, capability_id=str(cap))
        assert result is not None
        assert len(result["runs"]) == 2
        assert "manifests" in result["extractors_covered"]
        assert "licenses" in result["extractors_covered"]
        assert "interfaces" in result["extractors_missing"]
        assert "symbols" in result["extractors_missing"]

    def test_filters_by_extractor(self, conn):
        cap = _mk_capability(conn, f"pypi:filt-{uuid.uuid4().hex[:6]}", "filt")
        cv = _mk_version(conn, cap)
        conn.commit()

        r1 = queries.record_inspection_run(
            conn, capability_version_id=str(cv), extractor_name="manifests",
        )
        queries.finish_inspection_run(conn, run_id=r1["id"], status="completed")
        r2 = queries.record_inspection_run(
            conn, capability_version_id=str(cv), extractor_name="licenses",
        )
        queries.finish_inspection_run(conn, run_id=r2["id"], status="completed")

        result = queries.inspection_history(
            conn, capability_id=str(cap), extractor_name="manifests",
        )
        assert len(result["runs"]) == 1
        assert result["runs"][0]["extractor_name"] == "manifests"

    def test_empty_when_no_inspections(self, conn):
        cap = _mk_capability(conn, f"pypi:empty-{uuid.uuid4().hex[:6]}", "empty")
        _mk_version(conn, cap)
        conn.commit()

        result = queries.inspection_history(conn, capability_id=str(cap))
        assert result is not None
        assert result["runs"] == []
        assert len(result["extractors_missing"]) == 6

    def test_returns_none_on_bad_id(self, conn):
        result = queries.inspection_history(conn, capability_id="bad")
        assert result is None

    def test_returns_none_on_nonexistent(self, conn):
        result = queries.inspection_history(
            conn, capability_id=str(uuid.uuid4()),
        )
        assert result is None

    def test_orders_by_recency(self, conn):
        cap = _mk_capability(conn, f"pypi:order-{uuid.uuid4().hex[:6]}", "order")
        cv = _mk_version(conn, cap)
        conn.commit()

        r1 = queries.record_inspection_run(
            conn, capability_version_id=str(cv), extractor_name="manifests",
            extractor_version="1.0.0",
        )
        queries.finish_inspection_run(conn, run_id=r1["id"], status="completed")
        r2 = queries.record_inspection_run(
            conn, capability_version_id=str(cv), extractor_name="manifests",
            extractor_version="2.0.0",
        )
        queries.finish_inspection_run(conn, run_id=r2["id"], status="completed")

        result = queries.inspection_history(conn, capability_id=str(cap))
        assert result["runs"][0]["extractor_version"] == "2.0.0"
        assert result["runs"][1]["extractor_version"] == "1.0.0"
