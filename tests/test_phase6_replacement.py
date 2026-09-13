"""
Phase 6 — Failure-Driven Replacement tests.

Coverage:
  1. Failure diagnosis (pure function):
     - Maps failure_kind to category
     - Maps severity to urgency
     - Security failures boost urgency
     - Repeated failures escalate urgency
     - Root cause hints include summary
  2. Replacement criteria (pure function):
     - Excludes failed capability
     - Same-kind strategy keeps ecosystem + kind
     - Broader strategy drops component_kind
     - Cross-eco strategy drops both
     - Extracts search terms from display_name
  3. Candidate ranking (pure function):
     - Excludes specified IDs
     - Sorts by score descending
     - Adds experience bonus when available
  4. Replacement plan (pure function):
     - Critical urgency → replace_immediately
     - Medium urgency → replace_soon
     - Low urgency → monitor_and_plan
     - No candidates → no_alternatives_found
  5. DB-backed find_replacement query:
     - Returns diagnosis and candidates
     - Returns None for bad IDs
     - Returns empty candidates when no matches
"""
from __future__ import annotations

import uuid

import pytest

from core.project.replacement import (
    DiagnosisCategory,
    FailureDiagnosis,
    ReplacementCandidate,
    ReplacementCriteria,
    ReplacementStrategy,
    ReplacementUrgency,
    build_replacement_criteria,
    build_replacement_plan,
    diagnose_failure,
    rank_candidates,
    _extract_search_terms,
)
from mcp_server import queries


# ---------------------------------------------------------------------------
# Failure diagnosis — pure function tests
# ---------------------------------------------------------------------------

class TestDiagnoseFailure:

    def test_install_failure_is_compatibility(self):
        d = diagnose_failure("install_failure")
        assert d.category == "compatibility"

    def test_runtime_error_is_reliability(self):
        d = diagnose_failure("runtime_error")
        assert d.category == "reliability"

    def test_security_vulnerability_is_security(self):
        d = diagnose_failure("security_vulnerability")
        assert d.category == "security"

    def test_api_breaking_change(self):
        d = diagnose_failure("api_breaking_change")
        assert d.category == "api_change"

    def test_unknown_kind_is_other(self):
        d = diagnose_failure("unknown_thing")
        assert d.category == "other"

    def test_critical_severity_maps_to_urgency(self):
        d = diagnose_failure("other", severity="critical")
        assert d.urgency == "critical"

    def test_warning_severity_low_urgency(self):
        d = diagnose_failure("other", severity="warning")
        assert d.urgency == "low"

    def test_security_boosts_urgency(self):
        d = diagnose_failure("security_vulnerability", severity="warning")
        assert d.urgency in ("high", "critical")

    def test_repeated_failures_escalate(self):
        history = [{"failure_kind": "runtime_error"} for _ in range(5)]
        d = diagnose_failure("runtime_error", failure_history=history)
        assert d.urgency == "critical"

    def test_three_repeats_escalate_to_high(self):
        history = [{"failure_kind": "import_failure"} for _ in range(3)]
        d = diagnose_failure("import_failure", severity="warning",
                             failure_history=history)
        assert d.urgency in ("high", "critical")

    def test_root_cause_includes_summary(self):
        d = diagnose_failure("install_failure", summary="missing libpq")
        assert "missing libpq" in d.root_cause_hint

    def test_compatibility_strategy_same_kind(self):
        d = diagnose_failure("install_failure")
        assert d.recommended_strategy == "same_kind"

    def test_critical_security_strategy_cross_eco(self):
        d = diagnose_failure("security_vulnerability", severity="critical")
        assert d.recommended_strategy == "cross_eco"


class TestBuildReplacementCriteria:

    def test_excludes_failed_capability(self):
        cap = {"id": "cap-1", "ecosystem": "pypi",
               "component_kind": "library", "display_name": "httpx",
               "normalized_key": "pypi:httpx"}
        d = diagnose_failure("install_failure")
        criteria = build_replacement_criteria(cap, d)
        assert "cap-1" in criteria.exclude_ids

    def test_same_kind_keeps_ecosystem_and_kind(self):
        cap = {"id": "cap-1", "ecosystem": "pypi",
               "component_kind": "library", "display_name": "httpx",
               "normalized_key": "pypi:httpx"}
        d = FailureDiagnosis(
            failure_kind="install_failure", category="compatibility",
            urgency="medium", root_cause_hint="",
            recommended_strategy="same_kind",
        )
        criteria = build_replacement_criteria(cap, d)
        assert criteria.ecosystem == "pypi"
        assert criteria.component_kind == "library"

    def test_broader_drops_kind(self):
        cap = {"id": "cap-1", "ecosystem": "pypi",
               "component_kind": "library", "display_name": "httpx",
               "normalized_key": "pypi:httpx"}
        d = FailureDiagnosis(
            failure_kind="runtime_error", category="reliability",
            urgency="high", root_cause_hint="",
            recommended_strategy="broader",
        )
        criteria = build_replacement_criteria(cap, d)
        assert criteria.ecosystem == "pypi"
        assert criteria.component_kind is None

    def test_cross_eco_drops_both(self):
        cap = {"id": "cap-1", "ecosystem": "pypi",
               "component_kind": "library", "display_name": "httpx",
               "normalized_key": "pypi:httpx"}
        d = FailureDiagnosis(
            failure_kind="security_vulnerability", category="security",
            urgency="critical", root_cause_hint="",
            recommended_strategy="cross_eco",
        )
        criteria = build_replacement_criteria(cap, d)
        assert criteria.ecosystem is None
        assert criteria.component_kind is None

    def test_additional_excludes(self):
        cap = {"id": "cap-1", "ecosystem": "pypi",
               "component_kind": "library", "display_name": "x",
               "normalized_key": "pypi:x"}
        d = diagnose_failure("other")
        criteria = build_replacement_criteria(
            cap, d, additional_excludes=["cap-2", "cap-3"],
        )
        assert "cap-1" in criteria.exclude_ids
        assert "cap-2" in criteria.exclude_ids
        assert "cap-3" in criteria.exclude_ids


class TestExtractSearchTerms:

    def test_from_display_name(self):
        terms = _extract_search_terms("my-http-client", "pypi:my-http-client")
        assert "http" in terms
        assert "client" in terms

    def test_short_words_skipped(self):
        terms = _extract_search_terms("go db", "npm:go-db")
        assert "go" not in terms

    def test_empty(self):
        terms = _extract_search_terms("", "")
        assert terms == []


class TestRankCandidates:

    def test_excludes_ids(self):
        candidates = [
            {"id": "a", "normalized_key": "pypi:a", "display_name": "A",
             "total_score": 0.9},
            {"id": "b", "normalized_key": "pypi:b", "display_name": "B",
             "total_score": 0.8},
        ]
        ranked = rank_candidates(candidates, exclude_ids=["a"])
        assert len(ranked) == 1
        assert ranked[0].capability_id == "b"

    def test_sorts_by_score_descending(self):
        candidates = [
            {"id": "a", "normalized_key": "pypi:a", "display_name": "A",
             "total_score": 0.5},
            {"id": "b", "normalized_key": "pypi:b", "display_name": "B",
             "total_score": 0.9},
            {"id": "c", "normalized_key": "pypi:c", "display_name": "C",
             "total_score": 0.7},
        ]
        ranked = rank_candidates(candidates, exclude_ids=[])
        assert ranked[0].capability_id == "b"
        assert ranked[1].capability_id == "c"
        assert ranked[2].capability_id == "a"

    def test_experience_bonus(self):
        candidates = [
            {"id": "a", "normalized_key": "pypi:a", "display_name": "A",
             "total_score": 0.5},
            {"id": "b", "normalized_key": "pypi:b", "display_name": "B",
             "total_score": 0.5},
        ]
        scores = {"a": 0.9}
        ranked = rank_candidates(candidates, exclude_ids=[], project_scores=scores)
        assert ranked[0].capability_id == "a"
        assert ranked[0].score > ranked[1].score

    def test_empty_candidates(self):
        ranked = rank_candidates([], exclude_ids=[])
        assert ranked == []


class TestBuildReplacementPlan:

    def _cap(self):
        return {"id": "cap-1", "ecosystem": "pypi",
                "component_kind": "library", "display_name": "httpx",
                "normalized_key": "pypi:httpx"}

    def test_critical_replace_immediately(self):
        d = FailureDiagnosis(
            failure_kind="security_vulnerability", category="security",
            urgency="critical", root_cause_hint="CVE-2026-1234",
            recommended_strategy="cross_eco",
        )
        candidates = [ReplacementCandidate(
            capability_id="b", normalized_key="pypi:requests",
            display_name="requests", score=0.8, reason="registry score",
        )]
        plan = build_replacement_plan(self._cap(), d, candidates)
        assert plan.recommendation == "replace_immediately"

    def test_high_replace_immediately(self):
        d = FailureDiagnosis(
            failure_kind="runtime_error", category="reliability",
            urgency="high", root_cause_hint="",
            recommended_strategy="broader",
        )
        candidates = [ReplacementCandidate(
            capability_id="b", normalized_key="pypi:alt",
            display_name="alt", score=0.7, reason="registry score",
        )]
        plan = build_replacement_plan(self._cap(), d, candidates)
        assert plan.recommendation == "replace_immediately"

    def test_medium_replace_soon(self):
        d = FailureDiagnosis(
            failure_kind="install_failure", category="compatibility",
            urgency="medium", root_cause_hint="",
            recommended_strategy="same_kind",
        )
        candidates = [ReplacementCandidate(
            capability_id="b", normalized_key="pypi:alt",
            display_name="alt", score=0.7, reason="registry score",
        )]
        plan = build_replacement_plan(self._cap(), d, candidates)
        assert plan.recommendation == "replace_soon"

    def test_low_monitor(self):
        d = FailureDiagnosis(
            failure_kind="other", category="other",
            urgency="low", root_cause_hint="",
            recommended_strategy="same_kind",
        )
        candidates = [ReplacementCandidate(
            capability_id="b", normalized_key="pypi:alt",
            display_name="alt", score=0.7, reason="registry score",
        )]
        plan = build_replacement_plan(self._cap(), d, candidates)
        assert plan.recommendation == "monitor_and_plan"

    def test_no_candidates(self):
        d = FailureDiagnosis(
            failure_kind="install_failure", category="compatibility",
            urgency="high", root_cause_hint="",
            recommended_strategy="same_kind",
        )
        plan = build_replacement_plan(self._cap(), d, [])
        assert plan.recommendation == "no_alternatives_found"


# ---------------------------------------------------------------------------
# Seed helpers (DB tests)
# ---------------------------------------------------------------------------

def _mk_project(conn, name: str) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO project (name) VALUES (%s) RETURNING id",
            (name,),
        )
        return cur.fetchone()[0]


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
# DB-backed find_replacement tests
# ---------------------------------------------------------------------------

class TestFindReplacement:

    def test_returns_diagnosis_and_candidates(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        cap = _mk_capability(conn, "pypi:failing-lib", "failing-lib")
        _mk_version(conn, cap)
        conn.commit()

        result = queries.find_replacement(
            conn,
            project_id=str(proj),
            capability_id=str(cap),
            failure_kind="install_failure",
            severity="error",
            summary="missing system library",
        )
        assert result is not None
        assert result["normalized_key"] == "pypi:failing-lib"
        assert result["diagnosis"]["category"] == "compatibility"
        assert result["diagnosis"]["urgency"] in ("low", "medium", "high", "critical")
        assert result["recommendation"] in (
            "replace_immediately", "replace_soon",
            "monitor_and_plan", "no_alternatives_found",
        )
        assert isinstance(result["candidates"], list)

    def test_bad_ids_return_none(self, conn):
        result = queries.find_replacement(
            conn, project_id="bad", capability_id="bad",
        )
        assert result is None

    def test_nonexistent_capability_returns_none(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        conn.commit()
        fake_cap = str(uuid.uuid4())
        result = queries.find_replacement(
            conn, project_id=str(proj), capability_id=fake_cap,
        )
        assert result is None

    def test_excludes_failed_capability_from_candidates(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        cap = _mk_capability(conn, "pypi:broken-http", "broken-http")
        _mk_version(conn, cap)
        conn.commit()

        result = queries.find_replacement(
            conn,
            project_id=str(proj),
            capability_id=str(cap),
            failure_kind="runtime_error",
        )
        assert result is not None
        for c in result["candidates"]:
            assert c["capability_id"] != str(cap)

    def test_critical_security_gets_high_urgency(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        cap = _mk_capability(conn, "pypi:vuln-lib", "vuln-lib")
        _mk_version(conn, cap)
        conn.commit()

        result = queries.find_replacement(
            conn,
            project_id=str(proj),
            capability_id=str(cap),
            failure_kind="security_vulnerability",
            severity="critical",
        )
        assert result is not None
        assert result["diagnosis"]["urgency"] == "critical"
        assert result["diagnosis"]["category"] == "security"
