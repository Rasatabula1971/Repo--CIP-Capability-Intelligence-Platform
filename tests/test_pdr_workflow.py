"""
Tests for the PDR-to-Build workflow (vNext).

Covers all 9 tool contracts per the implementation spec §5:
  1. ingest_pdr            — PDR provenance root
  2. record_requirements   — validation + REQ-ID assignment
  3. search_for_requirement — fit evaluation retrieval
  4. record_decision       — verdict recording + search-before-build gate
  5. build_approval_brief  — read-only brief rendering
  6. lock_architecture     — versioned append-only lock
  7. check_against_lock    — lock-check query
  8. record_build_progress — task + verification recording
  9. coverage              — progress reporting
"""
from __future__ import annotations

import json
import uuid

import pytest

from mcp_server.pdr_queries import (
    build_approval_brief,
    check_against_lock,
    coverage,
    ingest_pdr,
    lock_architecture,
    record_build_progress,
    record_decision,
    record_requirements,
    search_for_requirement,
)
from mcp_server import queries


SAMPLE_PDR = """
# My App PDR

## Requirements
1. The app must have user authentication.
2. The app should support file uploads.
3. The app could have dark mode.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ingest(conn, name="test-project", pdr=SAMPLE_PDR):
    return ingest_pdr(
        conn, project_name=name, pdr_title="Test PDR", pdr_text=pdr,
    )


def _record_reqs(conn, project_id, source_id):
    return record_requirements(
        conn, project_id=project_id, requirement_source_id=source_id,
        requirements=[
            {
                "text": "User authentication with OAuth2",
                "priority": "must",
                "acceptance_criteria": "Users can sign in with Google OAuth",
                "source_span": {"start": 50, "end": 100, "quote": "user authentication"},
            },
            {
                "text": "File upload support up to 100MB",
                "priority": "should",
                "acceptance_criteria": "Users can upload files via drag-and-drop",
                "source_span": {"start": 101, "end": 150, "quote": "file uploads"},
            },
            {
                "text": "Dark mode toggle",
                "priority": "could",
                "acceptance_criteria": "UI switches between light and dark themes",
                "source_span": {"start": 151, "end": 200, "quote": "dark mode"},
            },
        ],
    )


def _setup_full(conn):
    """Ingest PDR + record requirements, return (project_id, source_id, req_ids)."""
    ing = _ingest(conn)
    pid = ing["project_id"]
    sid = ing["requirement_source_id"]
    rr = _record_reqs(conn, pid, sid)
    req_ids = [r["req_id"] for r in rr["requirements"] if r["valid"]]
    return pid, sid, req_ids


def _setup_with_decisions(conn):
    """Full setup + BUILD decisions for all reqs (with fake fit_evaluations)."""
    pid, sid, req_ids = _setup_full(conn)
    for rid in req_ids:
        _insert_fake_fit(conn, rid)
        record_decision(conn, req_id=rid, verdict="BUILD", rationale="No match found")
    return pid, sid, req_ids


def _insert_fake_fit(conn, req_id):
    """Insert a dummy fit_evaluation so BUILD verdicts pass the gate."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM capability_version LIMIT 1",
        )
        cv_row = cur.fetchone()
        if not cv_row:
            cur.execute(
                "INSERT INTO capability (normalized_key, display_name, ecosystem, kind) "
                "VALUES ('fake-cap', 'Fake', 'source', 'library') RETURNING id",
            )
            cap_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO capability_version (capability_id, version_key, version_kind) "
                "VALUES (%s, 'content:fake', 'content-hash') RETURNING id",
                (str(cap_id),),
            )
            cv_id = cur.fetchone()[0]
        else:
            cv_id = cv_row[0]

        cur.execute(
            """
            INSERT INTO fit_evaluation
                (project_requirement_id, capability_version_id, fit_score,
                 blocking_gap_count, computed_hash)
            VALUES (%s, %s, 0.1, 1, 'fakehash')
            ON CONFLICT DO NOTHING
            """,
            (req_id, str(cv_id)),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# 1. ingest_pdr
# ---------------------------------------------------------------------------

class TestIngestPdr:

    def test_creates_project_and_source(self, conn):
        result = _ingest(conn)
        assert result["project_id"]
        assert result["requirement_source_id"]
        assert result["content_hash"]
        assert result["was_duplicate"] is False

    def test_duplicate_returns_same_hash(self, conn):
        r1 = _ingest(conn)
        r2 = _ingest(conn)
        assert r1["content_hash"] == r2["content_hash"]
        assert r2["was_duplicate"] is True

    def test_different_pdr_different_source(self, conn):
        r1 = _ingest(conn, pdr="PDR v1")
        r2 = _ingest(conn, pdr="PDR v2")
        assert r1["requirement_source_id"] != r2["requirement_source_id"]

    def test_existing_project_id(self, conn):
        r1 = _ingest(conn)
        r2 = ingest_pdr(
            conn, project_name="ignored", pdr_title="Another",
            pdr_text="different text", project_id=r1["project_id"],
        )
        assert r2["project_id"] == r1["project_id"]

    def test_bad_project_id(self, conn):
        result = ingest_pdr(
            conn, project_name="x", pdr_title="x", pdr_text="x",
            project_id=str(uuid.uuid4()),
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# 2. record_requirements
# ---------------------------------------------------------------------------

class TestRecordRequirements:

    def test_valid_requirements(self, conn):
        ing = _ingest(conn)
        result = _record_reqs(conn, ing["project_id"], ing["requirement_source_id"])
        assert len(result["requirements"]) == 3
        assert all(r["valid"] for r in result["requirements"])
        assert all(r.get("req_id") for r in result["requirements"])

    def test_missing_acceptance_criteria(self, conn):
        ing = _ingest(conn)
        result = record_requirements(
            conn, project_id=ing["project_id"],
            requirement_source_id=ing["requirement_source_id"],
            requirements=[{
                "text": "Something",
                "priority": "must",
                "acceptance_criteria": "",
                "source_span": {"start": 0, "end": 10},
            }],
        )
        assert result["requirements"][0]["valid"] is False
        assert "acceptance_criteria" in result["requirements"][0]["errors"][0]

    def test_missing_text(self, conn):
        ing = _ingest(conn)
        result = record_requirements(
            conn, project_id=ing["project_id"],
            requirement_source_id=ing["requirement_source_id"],
            requirements=[{
                "text": "",
                "priority": "must",
                "acceptance_criteria": "test",
                "source_span": {"start": 0, "end": 10},
            }],
        )
        assert result["requirements"][0]["valid"] is False

    def test_invalid_priority(self, conn):
        ing = _ingest(conn)
        result = record_requirements(
            conn, project_id=ing["project_id"],
            requirement_source_id=ing["requirement_source_id"],
            requirements=[{
                "text": "Something",
                "priority": "critical",
                "acceptance_criteria": "test",
                "source_span": {"start": 0, "end": 10},
            }],
        )
        assert result["requirements"][0]["valid"] is False
        assert "priority" in result["requirements"][0]["errors"][0]

    def test_missing_source_span(self, conn):
        ing = _ingest(conn)
        result = record_requirements(
            conn, project_id=ing["project_id"],
            requirement_source_id=ing["requirement_source_id"],
            requirements=[{
                "text": "Something",
                "priority": "must",
                "acceptance_criteria": "test",
            }],
        )
        assert result["requirements"][0]["valid"] is False

    def test_bad_project_id(self, conn):
        result = record_requirements(
            conn, project_id=str(uuid.uuid4()),
            requirement_source_id=str(uuid.uuid4()),
            requirements=[],
        )
        assert "error" in result

    def test_upsert_on_slug_conflict(self, conn):
        ing = _ingest(conn)
        req = {
            "text": "Auth requirement", "slug": "auth",
            "priority": "must", "acceptance_criteria": "Login works",
            "source_span": {"start": 0, "end": 10},
        }
        r1 = record_requirements(
            conn, project_id=ing["project_id"],
            requirement_source_id=ing["requirement_source_id"],
            requirements=[req],
        )
        req["text"] = "Updated auth requirement"
        r2 = record_requirements(
            conn, project_id=ing["project_id"],
            requirement_source_id=ing["requirement_source_id"],
            requirements=[req],
        )
        assert r1["requirements"][0]["req_id"] == r2["requirements"][0]["req_id"]

    def test_with_constraints(self, conn):
        ing = _ingest(conn)
        result = record_requirements(
            conn, project_id=ing["project_id"],
            requirement_source_id=ing["requirement_source_id"],
            requirements=[{
                "text": "HTTP client library",
                "priority": "must",
                "acceptance_criteria": "Can make GET requests",
                "source_span": {"start": 0, "end": 10},
                "constraints": [
                    {"kind": "license_allowlist", "spdx_ids": ["MIT"]},
                    {"kind": "required_interface", "name": "get"},
                ],
            }],
        )
        assert result["requirements"][0]["valid"] is True


# ---------------------------------------------------------------------------
# 3. search_for_requirement
# ---------------------------------------------------------------------------

class TestSearchForRequirement:

    def test_returns_req_info(self, conn):
        pid, sid, req_ids = _setup_full(conn)
        result = search_for_requirement(conn, req_id=req_ids[0])
        assert result is not None
        assert result["req_id"] == req_ids[0]
        assert result["slug"]
        assert "candidates" in result

    def test_not_found(self, conn):
        result = search_for_requirement(conn, req_id=str(uuid.uuid4()))
        assert result is None

    def test_with_fit_evaluation(self, conn):
        pid, sid, req_ids = _setup_full(conn)
        _insert_fake_fit(conn, req_ids[0])
        result = search_for_requirement(conn, req_id=req_ids[0])
        assert result["candidate_count"] >= 1
        assert result["candidates"][0]["fit_score"] is not None


# ---------------------------------------------------------------------------
# 4. record_decision
# ---------------------------------------------------------------------------

class TestRecordDecision:

    def test_build_with_search(self, conn):
        pid, sid, req_ids = _setup_full(conn)
        _insert_fake_fit(conn, req_ids[0])
        result = record_decision(
            conn, req_id=req_ids[0], verdict="BUILD",
            rationale="No suitable library found",
        )
        assert result is not None
        assert result.get("recommendation_id")
        assert result["verdict"] == "BUILD"

    def test_build_without_search_refused(self, conn):
        pid, sid, req_ids = _setup_full(conn)
        result = record_decision(
            conn, req_id=req_ids[0], verdict="BUILD",
        )
        assert result["refused"] is True
        assert "search" in result["reason"].lower()

    def test_invalid_verdict(self, conn):
        pid, sid, req_ids = _setup_full(conn)
        result = record_decision(
            conn, req_id=req_ids[0], verdict="YOLO",
        )
        assert result["refused"] is True

    def test_reuse_requires_capability(self, conn):
        pid, sid, req_ids = _setup_full(conn)
        result = record_decision(
            conn, req_id=req_ids[0], verdict="ADOPT",
        )
        assert result["refused"] is True
        assert "chosen_capability_version_id" in result["reason"]

    def test_reject_no_search_needed(self, conn):
        pid, sid, req_ids = _setup_full(conn)
        result = record_decision(
            conn, req_id=req_ids[0], verdict="REJECT",
            rationale="Not needed for MVP",
        )
        assert result.get("recommendation_id")
        assert result["verdict"] == "REJECT"

    def test_not_found(self, conn):
        result = record_decision(
            conn, req_id=str(uuid.uuid4()), verdict="BUILD",
        )
        assert result is None


# ---------------------------------------------------------------------------
# 5. build_approval_brief
# ---------------------------------------------------------------------------

class TestBuildApprovalBrief:

    def test_renders_brief(self, conn):
        pid, sid, req_ids = _setup_full(conn)
        for rid in req_ids:
            _insert_fake_fit(conn, rid)
            record_decision(conn, req_id=rid, verdict="BUILD", rationale="custom build")
        result = build_approval_brief(conn, project_id=pid)
        assert result is not None
        assert "brief_markdown" in result
        assert len(result["decisions"]) == 3

    def test_unresolved_listed(self, conn):
        pid, sid, req_ids = _setup_full(conn)
        result = build_approval_brief(conn, project_id=pid)
        assert len(result["unresolved"]) == 3

    def test_not_found(self, conn):
        result = build_approval_brief(conn, project_id=str(uuid.uuid4()))
        assert result is None


# ---------------------------------------------------------------------------
# 6. lock_architecture
# ---------------------------------------------------------------------------

class TestLockArchitecture:

    def test_creates_lock(self, conn):
        pid, sid, req_ids = _setup_with_decisions(conn)
        result = lock_architecture(
            conn, project_id=pid, approved_by="engineer",
        )
        assert result is not None
        assert result["version"] == 1
        assert result["decision_count"] == 3
        assert result["source_lock_hash"]

    def test_refuses_undecided(self, conn):
        pid, sid, req_ids = _setup_full(conn)
        result = lock_architecture(
            conn, project_id=pid, approved_by="engineer",
        )
        assert result["refused"] is True
        assert "without decisions" in result["reason"]

    def test_versioning(self, conn):
        pid, sid, req_ids = _setup_with_decisions(conn)
        r1 = lock_architecture(conn, project_id=pid, approved_by="eng1")
        r2 = lock_architecture(conn, project_id=pid, approved_by="eng2")
        assert r1["version"] == 1
        assert r2["version"] == 2

    def test_only_one_active(self, conn):
        pid, sid, req_ids = _setup_with_decisions(conn)
        lock_architecture(conn, project_id=pid, approved_by="eng1")
        lock_architecture(conn, project_id=pid, approved_by="eng2")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM architecture_lock WHERE project_id = %s AND status = 'active'",
                (pid,),
            )
            assert cur.fetchone()[0] == 1

    def test_not_found(self, conn):
        result = lock_architecture(
            conn, project_id=str(uuid.uuid4()), approved_by="x",
        )
        assert result is None


# ---------------------------------------------------------------------------
# 7. check_against_lock
# ---------------------------------------------------------------------------

class TestCheckAgainstLock:

    def test_no_lock_allowed(self, conn):
        pid, sid, req_ids = _setup_full(conn)
        result = check_against_lock(
            conn, project_id=pid,
            proposed_change={"description": "Add a logger"},
        )
        assert result["verdict"] == "allowed"

    def test_allowed_change(self, conn):
        pid, sid, req_ids = _setup_with_decisions(conn)
        lock_architecture(conn, project_id=pid, approved_by="eng")
        result = check_against_lock(
            conn, project_id=pid,
            proposed_change={"description": "Refactor internal helper"},
        )
        assert result["verdict"] == "allowed"

    def test_locked_capability_change(self, conn):
        pid, sid, req_ids = _setup_with_decisions(conn)
        lock_architecture(conn, project_id=pid, approved_by="eng")
        result = check_against_lock(
            conn, project_id=pid,
            proposed_change={
                "capability_version_id": str(uuid.uuid4()),
                "description": "Swap to different library",
            },
        )
        # BUILD decisions don't have locked capability_version_ids
        # so this should be allowed
        assert result["verdict"] == "allowed"


# ---------------------------------------------------------------------------
# 8. record_build_progress
# ---------------------------------------------------------------------------

class TestRecordBuildProgress:

    def test_create_task(self, conn):
        pid, sid, req_ids = _setup_with_decisions(conn)
        lock = lock_architecture(conn, project_id=pid, approved_by="eng")
        result = record_build_progress(
            conn, project_id=pid,
            architecture_lock_id=lock["architecture_lock_id"],
            title="Implement auth", objective="Build OAuth2 login",
            req_ids=[req_ids[0]],
        )
        assert result["build_task_id"]
        assert result["status"] == "planned"

    def test_update_status(self, conn):
        pid, sid, req_ids = _setup_with_decisions(conn)
        lock = lock_architecture(conn, project_id=pid, approved_by="eng")
        created = record_build_progress(
            conn, project_id=pid,
            architecture_lock_id=lock["architecture_lock_id"],
            title="Build auth", objective="OAuth2",
            req_ids=[req_ids[0]],
        )
        updated = record_build_progress(
            conn, build_task_id=created["build_task_id"],
            status="in_progress",
        )
        assert updated["status"] == "in_progress"

    def test_record_verification(self, conn):
        pid, sid, req_ids = _setup_with_decisions(conn)
        lock = lock_architecture(conn, project_id=pid, approved_by="eng")
        created = record_build_progress(
            conn, project_id=pid,
            architecture_lock_id=lock["architecture_lock_id"],
            title="Build auth", objective="OAuth2",
            req_ids=[req_ids[0]],
        )
        result = record_build_progress(
            conn, build_task_id=created["build_task_id"],
            status="implemented", target_commit="abc123",
            verification={"req_id": req_ids[0], "outcome": "pass",
                          "evidence_ref": "test_auth.py::test_login"},
        )
        assert result["status"] == "implemented"
        with conn.cursor() as cur:
            cur.execute(
                "SELECT outcome FROM requirement_verification WHERE project_requirement_id = %s",
                (req_ids[0],),
            )
            assert cur.fetchone()[0] == "pass"

    def test_requires_req_ids(self, conn):
        pid, sid, req_ids = _setup_with_decisions(conn)
        lock = lock_architecture(conn, project_id=pid, approved_by="eng")
        result = record_build_progress(
            conn, project_id=pid,
            architecture_lock_id=lock["architecture_lock_id"],
            title="Orphan task", objective="nothing",
            req_ids=[],
        )
        assert "error" in result

    def test_bad_lock_id(self, conn):
        pid, sid, req_ids = _setup_with_decisions(conn)
        result = record_build_progress(
            conn, project_id=pid,
            architecture_lock_id=str(uuid.uuid4()),
            title="Bad", objective="bad",
            req_ids=[req_ids[0]],
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# 9. coverage
# ---------------------------------------------------------------------------

class TestCoverage:

    def test_empty_project(self, conn):
        ing = _ingest(conn)
        result = coverage(conn, project_id=ing["project_id"])
        assert result is not None
        assert result["total_requirements"] == 0

    def test_requirements_only(self, conn):
        pid, sid, req_ids = _setup_full(conn)
        result = coverage(conn, project_id=pid)
        assert result["total_requirements"] == 3
        assert result["decided"] == 0
        assert result["tasked"] == 0

    def test_with_decisions(self, conn):
        pid, sid, req_ids = _setup_with_decisions(conn)
        result = coverage(conn, project_id=pid)
        assert result["decided"] == 3
        assert result["tasked"] == 0

    def test_full_chain(self, conn):
        pid, sid, req_ids = _setup_with_decisions(conn)
        lock = lock_architecture(conn, project_id=pid, approved_by="eng")
        for rid in req_ids:
            task = record_build_progress(
                conn, project_id=pid,
                architecture_lock_id=lock["architecture_lock_id"],
                title=f"Task for {rid[:8]}", objective="build it",
                req_ids=[rid],
            )
            record_build_progress(
                conn, build_task_id=task["build_task_id"],
                status="implemented", target_commit="abc",
                verification={"req_id": rid, "outcome": "pass"},
            )
        result = coverage(conn, project_id=pid)
        assert result["total_requirements"] == 3
        assert result["decided"] == 3
        assert result["tasked"] == 3
        assert result["implemented"] == 3
        assert result["verified"] == 3

    def test_not_found(self, conn):
        result = coverage(conn, project_id=str(uuid.uuid4()))
        assert result is None

    def test_by_requirement_detail(self, conn):
        pid, sid, req_ids = _setup_with_decisions(conn)
        result = coverage(conn, project_id=pid)
        assert len(result["by_requirement"]) == 3
        for r in result["by_requirement"]:
            assert "slug" in r
            assert "verdict" in r
            assert r["verdict"] == "BUILD"


# ---------------------------------------------------------------------------
# End-to-end: full PDR workflow
# ---------------------------------------------------------------------------

class TestEndToEnd:

    def test_pdr_to_coverage(self, conn):
        """Full chain: PDR → reqs → decisions → lock → tasks → coverage."""
        # 1. Ingest PDR
        ing = _ingest(conn, name="e2e-project")
        pid = ing["project_id"]
        sid = ing["requirement_source_id"]

        # 2. Record requirements
        rr = _record_reqs(conn, pid, sid)
        req_ids = [r["req_id"] for r in rr["requirements"]]
        assert len(req_ids) == 3

        # 3. Search + decide
        for rid in req_ids:
            search = search_for_requirement(conn, req_id=rid)
            assert search is not None
            _insert_fake_fit(conn, rid)
            dec = record_decision(conn, req_id=rid, verdict="BUILD",
                                  rationale="Custom implementation")
            assert dec["verdict"] == "BUILD"

        # 4. Brief
        brief = build_approval_brief(conn, project_id=pid)
        assert len(brief["unresolved"]) == 0
        assert len(brief["decisions"]) == 3

        # 5. Lock
        lock = lock_architecture(conn, project_id=pid, approved_by="lead")
        assert lock["version"] == 1

        # 6. Check against lock
        check = check_against_lock(
            conn, project_id=pid,
            proposed_change={"description": "Internal refactor"},
        )
        assert check["verdict"] == "allowed"

        # 7. Build tasks + verification
        for rid in req_ids:
            task = record_build_progress(
                conn, project_id=pid,
                architecture_lock_id=lock["architecture_lock_id"],
                title=f"Build {rid[:8]}", objective="implement",
                req_ids=[rid],
            )
            record_build_progress(
                conn, build_task_id=task["build_task_id"],
                status="implemented", target_commit="sha256abc",
                verification={"req_id": rid, "outcome": "pass"},
            )

        # 8. Coverage
        cov = coverage(conn, project_id=pid)
        assert cov["total_requirements"] == 3
        assert cov["decided"] == 3
        assert cov["verified"] == 3


# ---------------------------------------------------------------------------
# Wired search+fit pipeline tests
# ---------------------------------------------------------------------------

def _setup_searchable_capability(conn, name="oauth-library", display="OAuth Library"):
    """Create a capability with full source binding + scorecard so it's
    searchable and _load_candidate_inputs can build a CandidateInputs."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_provider (name, kind) VALUES ('test-gh', 'code_host') "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id",
        )
        prov_id = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO source_asset (provider_id, external_key, display_name, kind) "
            "VALUES (%s, %s, %s, 'repository') RETURNING id",
            (prov_id, f"test/{name}", f"test/{name}"),
        )
        asset_id = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO source_revision (source_asset_id, revision_key) "
            "VALUES (%s, 'abc123') RETURNING id",
            (asset_id,),
        )
        rev_id = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO capability (normalized_key, display_name, ecosystem, kind, component_kind) "
            "VALUES (%s, %s, 'pypi', 'library', 'library') RETURNING id",
            (f"pypi:{name}", display),
        )
        cap_id = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO capability_version (capability_id, version_key, version_kind, display_version) "
            "VALUES (%s, 'content:v1', 'content-hash', '1.0.0') RETURNING id",
            (str(cap_id),),
        )
        cv_id = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO capability_source_binding (capability_version_id, source_revision_id) "
            "VALUES (%s, %s)",
            (str(cv_id), str(rev_id)),
        )

        cur.execute(
            "INSERT INTO scoring_profile (name, version, profile_hash, dimensions) "
            "VALUES (%s, 1, %s, '{}'::jsonb) RETURNING id",
            (f"prof-{uuid.uuid4().hex[:6]}", uuid.uuid4().hex),
        )
        profile_id = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO scorecard (capability_version_id, scoring_profile_id, "
            "total_score, confidence, computed_hash) "
            "VALUES (%s, %s, 0.85, 0.9, %s)",
            (str(cv_id), str(profile_id), uuid.uuid4().hex),
        )

    conn.commit()
    return str(cap_id), str(cv_id)


class TestWiredSearch:

    def test_search_finds_capability_and_creates_fit(self, conn):
        """search_for_requirement with a matching query should find the
        capability and create fit_evaluation rows."""
        cap_id, cv_id = _setup_searchable_capability(conn)
        pid, sid, req_ids = _setup_full(conn)

        result = search_for_requirement(
            conn, req_id=req_ids[0], search_query="OAuth",
        )
        assert result is not None
        assert result["search_query"] == "OAuth"
        assert result["candidate_count"] >= 1

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM fit_evaluation WHERE project_requirement_id = %s",
                (req_ids[0],),
            )
            assert cur.fetchone()[0] >= 1

    def test_search_returns_project_id(self, conn):
        pid, sid, req_ids = _setup_full(conn)
        result = search_for_requirement(conn, req_id=req_ids[0])
        assert result is not None
        assert result["project_id"] == pid

    def test_search_with_no_matches_returns_empty_candidates(self, conn):
        pid, sid, req_ids = _setup_full(conn)
        result = search_for_requirement(
            conn, req_id=req_ids[0], search_query="zzz_nonexistent_xyz",
        )
        assert result is not None
        assert result["candidate_count"] == 0

    def test_search_uses_description_as_default_query(self, conn):
        """When search_query is not provided, uses the requirement description."""
        cap_id, cv_id = _setup_searchable_capability(
            conn, name="file-uploader", display="File Uploader Library",
        )
        pid, sid, req_ids = _setup_full(conn)

        result = search_for_requirement(conn, req_id=req_ids[1])
        assert result is not None
        assert result["search_query"] is not None
        assert "File upload" in result["search_query"]

    def test_search_with_ecosystem_filter(self, conn):
        cap_id, cv_id = _setup_searchable_capability(conn)
        pid, sid, req_ids = _setup_full(conn)

        result = search_for_requirement(
            conn, req_id=req_ids[0], search_query="OAuth",
            ecosystem="npm",
        )
        assert result is not None
        assert result["candidate_count"] == 0

    def test_search_enables_build_decision(self, conn):
        """After search creates fit_evaluation, BUILD verdict should succeed."""
        cap_id, cv_id = _setup_searchable_capability(conn)
        pid, sid, req_ids = _setup_full(conn)

        search_for_requirement(
            conn, req_id=req_ids[0], search_query="OAuth",
        )

        dec = record_decision(
            conn, req_id=req_ids[0], verdict="BUILD",
            rationale="No good match, building custom",
        )
        assert dec is not None
        assert dec.get("refused") is None
        assert dec["verdict"] == "BUILD"

    def test_search_enables_adopt_decision(self, conn):
        """After search with a found capability, ADOPT verdict should work."""
        cap_id, cv_id = _setup_searchable_capability(conn)
        pid, sid, req_ids = _setup_full(conn)

        result = search_for_requirement(
            conn, req_id=req_ids[0], search_query="OAuth",
        )
        assert result["candidate_count"] >= 1

        dec = record_decision(
            conn, req_id=req_ids[0], verdict="ADOPT",
            chosen_capability_version_id=cv_id,
            rationale="Good fit",
        )
        assert dec is not None
        assert dec.get("refused") is None
        assert dec["verdict"] == "ADOPT"
