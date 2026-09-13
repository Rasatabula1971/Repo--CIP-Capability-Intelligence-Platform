"""
Phase 10 — Build-Gap Proof tests.

Coverage:
  1. authorize_build (query layer):
     - Authorizes a valid BUILD recommendation
     - Returns error on non-BUILD verdict
     - Returns error on missing search evidence
     - Returns None on bad recommendation_id
     - Returns error on empty authorized_by/reason
     - Idempotent — returns same auth_id on repeat
  2. build_gate_check (query layer):
     - Returns allowed=True after authorization
     - Returns allowed=False without recommendation
     - Returns allowed=False on non-BUILD verdict
     - Returns allowed=False on unauthorized BUILD
     - Returns None on bad requirement_id
  3. build_coverage (query layer):
     - Shows zero when no requirements
     - Shows correct counts with mixed verdicts
     - Returns None on bad project_id
     - Returns None on nonexistent project
"""
from __future__ import annotations

import json
import uuid

import pytest

from mcp_server import queries


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _mk_project(conn, name: str) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO project (name) VALUES (%s) "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "
            "RETURNING id",
            (name,),
        )
        pid = cur.fetchone()[0]
    conn.commit()
    return pid


def _mk_requirement(conn, project_id: uuid.UUID, slug: str) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO project_requirement (project_id, slug, description) "
            "VALUES (%s, %s, %s) RETURNING id",
            (project_id, slug, f"Requirement: {slug}"),
        )
        rid = cur.fetchone()[0]
    conn.commit()
    return rid


def _mk_capability(conn, key: str) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability "
            "(normalized_key, display_name, ecosystem, kind) "
            "VALUES (%s, %s, 'pypi', 'library') RETURNING id",
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


def _mk_rules_profile(conn) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recommendation_rules_profile "
            "(name, version, profile_hash, rules) "
            "VALUES ('test', 1, 'testhash', '[]'::jsonb) "
            "ON CONFLICT (name, version) DO UPDATE SET name = 'test' "
            "RETURNING id",
        )
        return cur.fetchone()[0]


def _mk_recommendation(
    conn,
    req_id: uuid.UUID,
    cv_id: uuid.UUID,
    verdict: str = "BUILD",
    rule_name: str = "__no_candidates__",
) -> uuid.UUID:
    profile_id = _mk_rules_profile(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recommendation "
            "(project_requirement_id, chosen_capability_version_id, "
            " rules_profile_id, verdict, reason, "
            " pinned_revision_key, pinned_source_asset_key, rule_name) "
            "VALUES (%s, %s, %s, %s, 'test reason', 'rev1', 'asset1', %s) "
            "RETURNING id",
            (req_id, cv_id, profile_id, verdict, rule_name),
        )
        rec_id = cur.fetchone()[0]
    conn.commit()
    return rec_id


# ---------------------------------------------------------------------------
# authorize_build
# ---------------------------------------------------------------------------

class TestAuthorizeBuild:

    def test_authorizes_valid_build(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        req = _mk_requirement(conn, proj, "auth-test")
        cap = _mk_capability(conn, f"pypi:ab-{uuid.uuid4().hex[:6]}")
        cv = _mk_version(conn, cap)
        conn.commit()
        rec = _mk_recommendation(conn, req, cv, "BUILD", "__no_candidates__")

        result = queries.authorize_build(
            conn,
            recommendation_id=str(rec),
            authorized_by="tester",
            reason="test authorization",
        )
        assert result is not None
        assert "error" not in result
        assert result["status"] == "authorized"
        assert result["authorized_by"] == "tester"

    def test_refuses_non_build(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        req = _mk_requirement(conn, proj, "non-build")
        cap = _mk_capability(conn, f"pypi:nb-{uuid.uuid4().hex[:6]}")
        cv = _mk_version(conn, cap)
        conn.commit()
        rec = _mk_recommendation(conn, req, cv, "ADOPT", "__no_candidates__")

        result = queries.authorize_build(
            conn,
            recommendation_id=str(rec),
            authorized_by="tester",
            reason="test",
        )
        assert result is not None
        assert "error" in result
        assert "BUILD" in result["error"]

    def test_refuses_missing_evidence(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        req = _mk_requirement(conn, proj, "no-evidence")
        cap = _mk_capability(conn, f"pypi:ne-{uuid.uuid4().hex[:6]}")
        cv = _mk_version(conn, cap)
        conn.commit()
        rec = _mk_recommendation(conn, req, cv, "BUILD", "best_score")

        result = queries.authorize_build(
            conn,
            recommendation_id=str(rec),
            authorized_by="tester",
            reason="test",
        )
        assert result is not None
        assert "error" in result
        assert "evidence" in result["error"].lower() or "search" in result["error"].lower()

    def test_returns_none_on_bad_id(self, conn):
        result = queries.authorize_build(
            conn,
            recommendation_id="bad",
            authorized_by="tester",
            reason="test",
        )
        assert result is None

    def test_error_on_empty_author(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        req = _mk_requirement(conn, proj, "empty-author")
        cap = _mk_capability(conn, f"pypi:ea-{uuid.uuid4().hex[:6]}")
        cv = _mk_version(conn, cap)
        conn.commit()
        rec = _mk_recommendation(conn, req, cv)

        result = queries.authorize_build(
            conn,
            recommendation_id=str(rec),
            authorized_by="",
            reason="test",
        )
        assert result is not None
        assert "error" in result

    def test_idempotent(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        req = _mk_requirement(conn, proj, "idempotent")
        cap = _mk_capability(conn, f"pypi:id-{uuid.uuid4().hex[:6]}")
        cv = _mk_version(conn, cap)
        conn.commit()
        rec = _mk_recommendation(conn, req, cv)

        r1 = queries.authorize_build(
            conn, recommendation_id=str(rec),
            authorized_by="tester", reason="first",
        )
        r2 = queries.authorize_build(
            conn, recommendation_id=str(rec),
            authorized_by="tester", reason="second",
        )
        assert r1["build_authorization_id"] == r2["build_authorization_id"]


# ---------------------------------------------------------------------------
# build_gate_check
# ---------------------------------------------------------------------------

class TestBuildGateCheck:

    def test_allowed_after_authorization(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        req = _mk_requirement(conn, proj, "gate-allowed")
        cap = _mk_capability(conn, f"pypi:ga-{uuid.uuid4().hex[:6]}")
        cv = _mk_version(conn, cap)
        conn.commit()
        rec = _mk_recommendation(conn, req, cv)
        queries.authorize_build(
            conn, recommendation_id=str(rec),
            authorized_by="tester", reason="test",
        )

        result = queries.build_gate_check(conn, project_requirement_id=str(req))
        assert result is not None
        assert result["allowed"] is True

    def test_denied_without_recommendation(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        req = _mk_requirement(conn, proj, "gate-no-rec")

        result = queries.build_gate_check(conn, project_requirement_id=str(req))
        assert result is not None
        assert result["allowed"] is False
        assert "no recommendation" in result["reason"]

    def test_denied_non_build(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        req = _mk_requirement(conn, proj, "gate-adopt")
        cap = _mk_capability(conn, f"pypi:gnb-{uuid.uuid4().hex[:6]}")
        cv = _mk_version(conn, cap)
        conn.commit()
        _mk_recommendation(conn, req, cv, "ADOPT")

        result = queries.build_gate_check(conn, project_requirement_id=str(req))
        assert result is not None
        assert result["allowed"] is False

    def test_denied_unauthorized_build(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        req = _mk_requirement(conn, proj, "gate-unauth")
        cap = _mk_capability(conn, f"pypi:gu-{uuid.uuid4().hex[:6]}")
        cv = _mk_version(conn, cap)
        conn.commit()
        _mk_recommendation(conn, req, cv, "BUILD")

        result = queries.build_gate_check(conn, project_requirement_id=str(req))
        assert result is not None
        assert result["allowed"] is False
        assert "not authorized" in result["reason"]

    def test_returns_none_on_bad_id(self, conn):
        result = queries.build_gate_check(conn, project_requirement_id="bad")
        assert result is None


# ---------------------------------------------------------------------------
# build_coverage
# ---------------------------------------------------------------------------

class TestBuildCoverage:

    def test_empty_project(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        result = queries.build_coverage(conn, project_id=str(proj))
        assert result is not None
        assert result["total_requirements"] == 0
        assert result["build_verdicts"] == 0
        assert result["authorized"] == 0

    def test_mixed_verdicts(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        req1 = _mk_requirement(conn, proj, "cov-build")
        req2 = _mk_requirement(conn, proj, "cov-adopt")
        req3 = _mk_requirement(conn, proj, "cov-uncovered")

        cap = _mk_capability(conn, f"pypi:cov-{uuid.uuid4().hex[:6]}")
        cv = _mk_version(conn, cap)
        conn.commit()

        rec1 = _mk_recommendation(conn, req1, cv, "BUILD")
        _mk_recommendation(conn, req2, cv, "ADOPT")

        queries.authorize_build(
            conn, recommendation_id=str(rec1),
            authorized_by="tester", reason="needed",
        )

        result = queries.build_coverage(conn, project_id=str(proj))
        assert result["total_requirements"] == 3
        assert result["build_verdicts"] == 1
        assert result["authorized"] == 1
        assert result["authorization_pct"] == 100.0

        slugs = {r["slug"]: r for r in result["requirements"]}
        assert slugs["cov-build"]["is_build"] is True
        assert slugs["cov-build"]["has_authorization"] is True
        assert slugs["cov-adopt"]["is_build"] is False
        assert slugs["cov-uncovered"]["has_recommendation"] is False

    def test_returns_none_on_bad_id(self, conn):
        result = queries.build_coverage(conn, project_id="bad")
        assert result is None

    def test_returns_none_on_nonexistent(self, conn):
        result = queries.build_coverage(conn, project_id=str(uuid.uuid4()))
        assert result is None
