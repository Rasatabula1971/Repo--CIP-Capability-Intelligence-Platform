"""
Phase 2 — Dependency Intelligence tests.

Coverage:
  1. Version comparison and satisfaction:
     - Basic version_satisfies checks (>=, <=, >, <, ==, !=, ~=)
  2. Dependency fit evaluator (pure function):
     - runtime_version pass/fail/warning
     - os match/mismatch
     - system_library present/missing
     - service present/missing
     - environment_var present/missing
     - hardware gpu/ram/arch checks
     - optional (non-required) facts produce warnings not hard failures
     - empty profile produces warnings
     - empty facts pass
  3. Version conflict detection:
     - No conflict when ranges overlap
     - Conflict when ranges don't overlap
     - No conflict with empty specs
  4. DB-backed dependency_fit query:
     - Returns result for capability with facts
     - Returns result for capability with no facts
     - Returns None for bad id
     - Checks against environment_profile
  5. DB-backed dependency_conflicts query:
     - No conflicts between compatible deps
     - Returns None for bad id
"""
from __future__ import annotations

import uuid

import pytest

from core.policy.dependency import (
    DependencyFact,
    DependencyFitResult,
    DependencyFinding,
    Severity,
    detect_version_conflicts,
    evaluate_dependency_fit,
    version_satisfies,
)
from mcp_server import queries


# ---------------------------------------------------------------------------
# Version satisfaction — pure function tests
# ---------------------------------------------------------------------------

class TestVersionSatisfies:

    def test_ge_satisfied(self):
        assert version_satisfies("3.11.4", ">=3.9") is True

    def test_ge_not_satisfied(self):
        assert version_satisfies("3.8.1", ">=3.9") is False

    def test_ge_exact_boundary(self):
        assert version_satisfies("3.9", ">=3.9") is True

    def test_le_satisfied(self):
        assert version_satisfies("3.9", "<=3.11") is True

    def test_le_not_satisfied(self):
        assert version_satisfies("3.12", "<=3.11") is False

    def test_gt_satisfied(self):
        assert version_satisfies("3.10", ">3.9") is True

    def test_gt_not_satisfied(self):
        assert version_satisfies("3.9", ">3.9") is False

    def test_lt_satisfied(self):
        assert version_satisfies("3.8", "<3.9") is True

    def test_lt_not_satisfied(self):
        assert version_satisfies("3.9", "<3.9") is False

    def test_eq_satisfied(self):
        assert version_satisfies("3.11.0", "==3.11.0") is True

    def test_eq_not_satisfied(self):
        assert version_satisfies("3.11.1", "==3.11.0") is False

    def test_ne_satisfied(self):
        assert version_satisfies("3.11.1", "!=3.11.0") is True

    def test_ne_not_satisfied(self):
        assert version_satisfies("3.11.0", "!=3.11.0") is False

    def test_compound_spec(self):
        assert version_satisfies("3.10.5", ">=3.9,<3.12") is True

    def test_compound_spec_fail_upper(self):
        assert version_satisfies("3.12.0", ">=3.9,<3.12") is False

    def test_empty_spec_passes(self):
        assert version_satisfies("3.11", "") is True

    def test_no_version_fails(self):
        assert version_satisfies("", ">=3.9") is False


# ---------------------------------------------------------------------------
# Dependency fit evaluator — pure function tests
# ---------------------------------------------------------------------------

class TestEvaluateDependencyFit:

    def test_empty_facts_passes(self):
        result = evaluate_dependency_fit([], {"runtimes": {"python": "3.11"}})
        assert result.passed is True
        assert result.hard_failures == []
        assert result.warnings == []

    def test_runtime_version_satisfied(self):
        facts = [DependencyFact("runtime_version", "python", ">=3.9")]
        profile = {"runtimes": {"python": "3.11.4"}}
        result = evaluate_dependency_fit(facts, profile)
        assert result.passed is True
        assert result.hard_failures == []

    def test_runtime_version_not_satisfied(self):
        facts = [DependencyFact("runtime_version", "python", ">=3.11")]
        profile = {"runtimes": {"python": "3.9.1"}}
        result = evaluate_dependency_fit(facts, profile)
        assert result.passed is False
        assert len(result.hard_failures) == 1
        assert result.hard_failures[0].reason == "version_mismatch"

    def test_runtime_not_declared_is_warning(self):
        facts = [DependencyFact("runtime_version", "python", ">=3.9")]
        profile = {"runtimes": {}}
        result = evaluate_dependency_fit(facts, profile)
        assert result.passed is True
        assert len(result.warnings) == 1
        assert result.warnings[0].reason == "runtime_not_declared"

    def test_os_match(self):
        facts = [DependencyFact("os", "linux")]
        profile = {"os": ["linux", "darwin"]}
        result = evaluate_dependency_fit(facts, profile)
        assert result.passed is True

    def test_os_mismatch(self):
        facts = [DependencyFact("os", "linux")]
        profile = {"os": ["darwin"]}
        result = evaluate_dependency_fit(facts, profile)
        assert result.passed is False
        assert result.hard_failures[0].reason == "os_mismatch"

    def test_os_not_declared_is_warning(self):
        facts = [DependencyFact("os", "linux")]
        profile = {}
        result = evaluate_dependency_fit(facts, profile)
        assert result.passed is True
        assert len(result.warnings) == 1

    def test_system_library_present(self):
        facts = [DependencyFact("system_library", "libpq")]
        profile = {"system_libraries": ["libpq", "libssl"]}
        result = evaluate_dependency_fit(facts, profile)
        assert result.passed is True

    def test_system_library_missing(self):
        facts = [DependencyFact("system_library", "ffmpeg")]
        profile = {"system_libraries": ["libpq"]}
        result = evaluate_dependency_fit(facts, profile)
        assert result.passed is False
        assert result.hard_failures[0].reason == "missing_system_library"

    def test_service_present(self):
        facts = [DependencyFact("service", "postgresql")]
        profile = {"services": ["postgresql", "redis"]}
        result = evaluate_dependency_fit(facts, profile)
        assert result.passed is True

    def test_service_missing(self):
        facts = [DependencyFact("service", "redis")]
        profile = {"services": ["postgresql"]}
        result = evaluate_dependency_fit(facts, profile)
        assert result.passed is False
        assert result.hard_failures[0].reason == "missing_service"

    def test_env_var_present(self):
        facts = [DependencyFact("environment_var", "DATABASE_URL")]
        profile = {"environment_vars": ["DATABASE_URL", "API_KEY"]}
        result = evaluate_dependency_fit(facts, profile)
        assert result.passed is True

    def test_env_var_missing(self):
        facts = [DependencyFact("environment_var", "SECRET_KEY")]
        profile = {"environment_vars": ["DATABASE_URL"]}
        result = evaluate_dependency_fit(facts, profile)
        assert result.passed is False

    def test_hardware_gpu_required_but_absent(self):
        facts = [DependencyFact("hardware", "gpu")]
        profile = {"hardware": {"gpu": False, "min_ram_gb": 8}}
        result = evaluate_dependency_fit(facts, profile)
        assert result.passed is False
        assert result.hard_failures[0].reason == "no_gpu"

    def test_hardware_gpu_required_and_present(self):
        facts = [DependencyFact("hardware", "gpu")]
        profile = {"hardware": {"gpu": True}}
        result = evaluate_dependency_fit(facts, profile)
        assert result.passed is True

    def test_hardware_ram_sufficient(self):
        facts = [DependencyFact("hardware", "min_ram_gb:8")]
        profile = {"hardware": {"min_ram_gb": 16}}
        result = evaluate_dependency_fit(facts, profile)
        assert result.passed is True

    def test_hardware_ram_insufficient(self):
        facts = [DependencyFact("hardware", "min_ram_gb:16")]
        profile = {"hardware": {"min_ram_gb": 8}}
        result = evaluate_dependency_fit(facts, profile)
        assert result.passed is False
        assert result.hard_failures[0].reason == "insufficient_ram"

    def test_hardware_arch_match(self):
        facts = [DependencyFact("hardware", "arch:x86_64")]
        profile = {"arch": ["x86_64", "aarch64"]}
        result = evaluate_dependency_fit(facts, profile)
        assert result.passed is True

    def test_hardware_arch_mismatch(self):
        facts = [DependencyFact("hardware", "arch:aarch64")]
        profile = {"arch": ["x86_64"]}
        result = evaluate_dependency_fit(facts, profile)
        assert result.passed is False

    def test_optional_fact_produces_warning_not_hard_failure(self):
        facts = [DependencyFact("service", "redis", required=False)]
        profile = {"services": ["postgresql"]}
        result = evaluate_dependency_fit(facts, profile)
        assert result.passed is True
        assert len(result.warnings) == 1
        assert result.warnings[0].reason == "missing_service"

    def test_multiple_facts_mixed(self):
        facts = [
            DependencyFact("runtime_version", "python", ">=3.9"),
            DependencyFact("os", "linux"),
            DependencyFact("service", "postgresql"),
        ]
        profile = {
            "runtimes": {"python": "3.11"},
            "os": ["linux"],
            "services": ["postgresql"],
        }
        result = evaluate_dependency_fit(facts, profile)
        assert result.passed is True
        assert result.hard_failures == []

    def test_multiple_facts_one_fails(self):
        facts = [
            DependencyFact("runtime_version", "python", ">=3.11"),
            DependencyFact("os", "darwin"),
        ]
        profile = {
            "runtimes": {"python": "3.9"},
            "os": ["darwin"],
        }
        result = evaluate_dependency_fit(facts, profile)
        assert result.passed is False
        assert len(result.hard_failures) == 1

    def test_empty_profile_all_warnings(self):
        facts = [
            DependencyFact("runtime_version", "python", ">=3.9"),
            DependencyFact("os", "linux"),
        ]
        result = evaluate_dependency_fit(facts, {})
        assert result.passed is True
        assert len(result.warnings) == 2


# ---------------------------------------------------------------------------
# Version conflict detection — pure function tests
# ---------------------------------------------------------------------------

class TestVersionConflicts:

    def test_no_conflict_overlapping_ranges(self):
        a = [{"ecosystem": "pypi", "name": "httpx", "version_spec": ">=0.24,<1.0"}]
        b = [{"ecosystem": "pypi", "name": "httpx", "version_spec": ">=0.25,<0.28"}]
        conflicts = detect_version_conflicts(a, b)
        assert conflicts == []

    def test_conflict_non_overlapping_ranges(self):
        a = [{"ecosystem": "pypi", "name": "requests", "version_spec": ">=3.0"}]
        b = [{"ecosystem": "pypi", "name": "requests", "version_spec": "<2.0"}]
        conflicts = detect_version_conflicts(a, b)
        assert len(conflicts) == 1
        assert conflicts[0]["name"] == "requests"

    def test_no_conflict_empty_specs(self):
        a = [{"ecosystem": "pypi", "name": "click", "version_spec": ""}]
        b = [{"ecosystem": "pypi", "name": "click", "version_spec": ">=8.0"}]
        conflicts = detect_version_conflicts(a, b)
        assert conflicts == []

    def test_no_conflict_different_packages(self):
        a = [{"ecosystem": "pypi", "name": "httpx", "version_spec": ">=0.24"}]
        b = [{"ecosystem": "pypi", "name": "requests", "version_spec": ">=2.0"}]
        conflicts = detect_version_conflicts(a, b)
        assert conflicts == []

    def test_conflict_cross_ecosystem_no_match(self):
        a = [{"ecosystem": "pypi", "name": "lodash", "version_spec": ">=4.0"}]
        b = [{"ecosystem": "npm", "name": "lodash", "version_spec": "<3.0"}]
        conflicts = detect_version_conflicts(a, b)
        assert conflicts == []


# ---------------------------------------------------------------------------
# Seed helpers (DB tests)
# ---------------------------------------------------------------------------

def _mk_provider_asset_revision(conn) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_provider (name, kind) VALUES ('fake', 'code_host') "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id"
        )
        provider_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO source_asset (provider_id, external_key, display_name, kind) "
            "VALUES (%s, %s, %s, 'repository') RETURNING id",
            (provider_id, f"d/{uuid.uuid4().hex[:6]}", "repo"),
        )
        asset_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO source_revision (source_asset_id, revision_key) "
            "VALUES (%s, %s) RETURNING id",
            (asset_id, uuid.uuid4().hex),
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


def _add_dep_fact(
    conn, version_id: uuid.UUID,
    fact_kind: str, fact_key: str,
    version_spec: str = "", required: bool = True,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO dependency_fact "
            "(capability_version_id, fact_kind, fact_key, version_spec, required) "
            "VALUES (%s, %s, %s, %s, %s)",
            (version_id, fact_kind, fact_key, version_spec, required),
        )


def _add_pkg_dep(
    conn, version_id: uuid.UUID, evidence_item_id: uuid.UUID,
    ecosystem: str, name: str, version_spec: str = "",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability_dependency "
            "(capability_version_id, evidence_item_id, "
            " depends_on_ecosystem, depends_on_name, version_spec, dep_kind) "
            "VALUES (%s, %s, %s, %s, %s, 'runtime')",
            (version_id, evidence_item_id, ecosystem, name, version_spec),
        )


def _mk_evidence(conn, revision_id: uuid.UUID) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_run "
            "(source_revision_id, extractor_config_version, status) "
            "VALUES (%s, 1, 'completed') RETURNING id",
            (revision_id,),
        )
        run_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO evidence_item "
            "(source_revision_id, analysis_run_id, extractor_name, "
            " evidence_type, locator_kind, locator, extracted_value) "
            "VALUES (%s, %s, 'test', 'dependency', 'manifest_key', "
            " '{\"path\":\"pyproject.toml\"}'::jsonb, '{}'::jsonb) RETURNING id",
            (revision_id, run_id),
        )
        return cur.fetchone()[0]


def _mk_project(conn, name: str) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO project (name) VALUES (%s) RETURNING id",
            (name,),
        )
        return cur.fetchone()[0]


def _mk_env_profile(
    conn, project_id: uuid.UUID, profile: dict,
    name: str = "default",
) -> uuid.UUID:
    import json
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO environment_profile (project_id, name, profile) "
            "VALUES (%s, %s, %s::jsonb) RETURNING id",
            (project_id, name, json.dumps(profile)),
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# DB-backed dependency_fit tests
# ---------------------------------------------------------------------------

class TestDependencyFitQuery:

    def test_capability_with_facts_against_profile(self, conn):
        cap = _mk_capability(conn, "pypi:dep-test-a", "dep-test-a")
        ver = _mk_version(conn, cap)
        _add_dep_fact(conn, ver, "runtime_version", "python", ">=3.9")
        _add_dep_fact(conn, ver, "service", "postgresql")

        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        _mk_env_profile(conn, proj, {
            "runtimes": {"python": "3.11.4"},
            "services": ["postgresql", "redis"],
        })
        conn.commit()

        result = queries.dependency_fit(conn, str(cap), str(proj))
        assert result is not None
        assert result["passed"] is True
        assert result["hard_failures"] == []

    def test_capability_with_facts_fails_against_profile(self, conn):
        cap = _mk_capability(conn, "pypi:dep-test-b", "dep-test-b")
        ver = _mk_version(conn, cap)
        _add_dep_fact(conn, ver, "runtime_version", "python", ">=3.11")
        _add_dep_fact(conn, ver, "os", "linux")
        conn.commit()

        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        _mk_env_profile(conn, proj, {
            "runtimes": {"python": "3.9.1"},
            "os": ["darwin"],
        })
        conn.commit()

        result = queries.dependency_fit(conn, str(cap), str(proj))
        assert result is not None
        assert result["passed"] is False
        assert len(result["hard_failures"]) == 2

    def test_capability_with_no_facts(self, conn):
        cap = _mk_capability(conn, "pypi:dep-test-c", "dep-test-c")
        _mk_version(conn, cap)
        conn.commit()

        result = queries.dependency_fit(conn, str(cap))
        assert result is not None
        assert result["passed"] is True
        assert result["hard_failures"] == []
        assert result["warnings"] == []

    def test_returns_none_for_bad_id(self, conn):
        result = queries.dependency_fit(conn, "not-a-uuid")
        assert result is None

    def test_includes_package_deps(self, conn):
        cap = _mk_capability(conn, "pypi:dep-test-d", "dep-test-d")
        ver = _mk_version(conn, cap)
        rev = _mk_provider_asset_revision(conn)
        ev = _mk_evidence(conn, rev)
        _add_pkg_dep(conn, ver, ev, "pypi", "httpx", ">=0.24")
        _add_pkg_dep(conn, ver, ev, "pypi", "pydantic", ">=2.0")
        conn.commit()

        result = queries.dependency_fit(conn, str(cap))
        assert result is not None
        assert len(result["package_deps"]) == 2
        names = {d["name"] for d in result["package_deps"]}
        assert "httpx" in names
        assert "pydantic" in names

    def test_no_head_version(self, conn):
        cap = _mk_capability(conn, "pypi:dep-test-e", "dep-test-e")
        conn.commit()
        result = queries.dependency_fit(conn, str(cap))
        assert result is not None
        assert result["passed"] is True
        assert len(result["warnings"]) == 1


# ---------------------------------------------------------------------------
# DB-backed dependency_conflicts tests
# ---------------------------------------------------------------------------

class TestDependencyConflictsQuery:

    def test_no_conflicts(self, conn):
        cap_a = _mk_capability(conn, "pypi:conf-a", "conf-a")
        cap_b = _mk_capability(conn, "pypi:conf-b", "conf-b")
        ver_a = _mk_version(conn, cap_a)
        ver_b = _mk_version(conn, cap_b)
        rev = _mk_provider_asset_revision(conn)
        ev = _mk_evidence(conn, rev)
        _add_pkg_dep(conn, ver_a, ev, "pypi", "httpx", ">=0.24,<1.0")
        _add_pkg_dep(conn, ver_b, ev, "pypi", "httpx", ">=0.25")
        conn.commit()

        result = queries.dependency_conflicts(conn, str(cap_a), str(cap_b))
        assert result is not None
        assert result["conflicts"] == []

    def test_conflict_detected(self, conn):
        cap_a = _mk_capability(conn, "pypi:conf-c", "conf-c")
        cap_b = _mk_capability(conn, "pypi:conf-d", "conf-d")
        ver_a = _mk_version(conn, cap_a)
        ver_b = _mk_version(conn, cap_b)
        rev = _mk_provider_asset_revision(conn)
        ev = _mk_evidence(conn, rev)
        _add_pkg_dep(conn, ver_a, ev, "pypi", "requests", ">=3.0")
        _add_pkg_dep(conn, ver_b, ev, "pypi", "requests", "<2.0")
        conn.commit()

        result = queries.dependency_conflicts(conn, str(cap_a), str(cap_b))
        assert result is not None
        assert len(result["conflicts"]) == 1
        assert result["conflicts"][0]["name"] == "requests"

    def test_returns_none_for_bad_id(self, conn):
        result = queries.dependency_conflicts(conn, "bad", "also-bad")
        assert result is None
