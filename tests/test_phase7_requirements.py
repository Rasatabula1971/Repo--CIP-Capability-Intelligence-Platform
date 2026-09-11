"""
Phase 7 — Requirement Traceability tests.

Coverage:
  1. YAML parser (pure function):
     - Parses valid YAML with all constraint kinds
     - Rejects invalid YAML (not a mapping, missing fields, duplicates)
     - Validates constraint-kind-specific fields
     - Converts to domain objects
  2. Import requirements query (DB-backed):
     - Creates new requirements + constraints
     - Updates existing requirements (upsert by slug)
     - Returns error on malformed YAML
     - Returns None on bad project ID
  3. Requirement coverage report (DB-backed):
     - Empty project has 0% coverage
     - Requirements without recommendations = uncovered
     - Requirements with recommendations = covered
     - Returns None on bad project ID
"""
from __future__ import annotations

import uuid

import pytest

from core.project.requirements import (
    RequirementParseError,
    parse_requirements_yaml,
    parse_requirements_file,
    to_domain_objects,
)
from mcp_server import queries


# ---------------------------------------------------------------------------
# YAML parser — pure function tests
# ---------------------------------------------------------------------------

VALID_YAML = """\
requirements:
  - slug: http-client
    description: "Need an HTTP client library"
    constraints:
      - kind: required_interface
        name: get
        signature_contains: "url"
      - kind: license_allowlist
        spdx_ids: [MIT, Apache-2.0]
  - slug: no-urllib3
    description: "Must not depend on urllib3"
    constraints:
      - kind: forbidden_dependency
        ecosystem: pypi
        name: urllib3
"""


class TestParseRequirementsYaml:

    def test_parses_valid_yaml(self):
        result = parse_requirements_yaml(VALID_YAML)
        assert len(result) == 2
        assert result[0]["slug"] == "http-client"
        assert result[0]["description"] == "Need an HTTP client library"
        assert len(result[0]["constraints"]) == 2
        assert result[0]["constraints"][0]["kind"] == "required_interface"
        assert result[0]["constraints"][0]["detail"]["name"] == "get"
        assert result[0]["constraints"][0]["detail"]["signature_contains"] == "url"
        assert result[0]["constraints"][1]["kind"] == "license_allowlist"
        assert result[0]["constraints"][1]["detail"]["spdx_ids"] == ["MIT", "Apache-2.0"]

    def test_parses_forbidden_dependency(self):
        result = parse_requirements_yaml(VALID_YAML)
        c = result[1]["constraints"][0]
        assert c["kind"] == "forbidden_dependency"
        assert c["detail"]["ecosystem"] == "pypi"
        assert c["detail"]["name"] == "urllib3"

    def test_no_constraints_ok(self):
        yaml_text = """\
requirements:
  - slug: simple
    description: "A simple requirement"
"""
        result = parse_requirements_yaml(yaml_text)
        assert len(result) == 1
        assert result[0]["constraints"] == []

    def test_rejects_non_mapping(self):
        with pytest.raises(RequirementParseError, match="top-level must be a mapping"):
            parse_requirements_yaml("- item1\n- item2\n")

    def test_rejects_missing_requirements_key(self):
        with pytest.raises(RequirementParseError, match="'requirements' must be a list"):
            parse_requirements_yaml("something_else: true\n")

    def test_rejects_empty_requirements(self):
        with pytest.raises(RequirementParseError, match="empty"):
            parse_requirements_yaml("requirements: []\n")

    def test_rejects_duplicate_slug(self):
        yaml_text = """\
requirements:
  - slug: dup
    description: "first"
  - slug: dup
    description: "second"
"""
        with pytest.raises(RequirementParseError, match="duplicate slug"):
            parse_requirements_yaml(yaml_text)

    def test_rejects_missing_slug(self):
        yaml_text = """\
requirements:
  - description: "no slug here"
"""
        with pytest.raises(RequirementParseError, match="'slug' must be a non-empty string"):
            parse_requirements_yaml(yaml_text)

    def test_rejects_invalid_constraint_kind(self):
        yaml_text = """\
requirements:
  - slug: bad
    constraints:
      - kind: nonexistent_kind
"""
        with pytest.raises(RequirementParseError, match="'kind' must be one of"):
            parse_requirements_yaml(yaml_text)

    def test_rejects_required_interface_without_name(self):
        yaml_text = """\
requirements:
  - slug: bad
    constraints:
      - kind: required_interface
"""
        with pytest.raises(RequirementParseError, match="needs 'name'"):
            parse_requirements_yaml(yaml_text)

    def test_rejects_license_allowlist_empty_spdx(self):
        yaml_text = """\
requirements:
  - slug: bad
    constraints:
      - kind: license_allowlist
        spdx_ids: []
"""
        with pytest.raises(RequirementParseError, match="non-empty 'spdx_ids'"):
            parse_requirements_yaml(yaml_text)

    def test_rejects_forbidden_dep_without_ecosystem(self):
        yaml_text = """\
requirements:
  - slug: bad
    constraints:
      - kind: forbidden_dependency
        name: foo
"""
        with pytest.raises(RequirementParseError, match="needs 'ecosystem'"):
            parse_requirements_yaml(yaml_text)

    def test_rejects_forbidden_dep_without_name(self):
        yaml_text = """\
requirements:
  - slug: bad
    constraints:
      - kind: forbidden_dependency
        ecosystem: pypi
"""
        with pytest.raises(RequirementParseError, match="needs 'name'"):
            parse_requirements_yaml(yaml_text)

    def test_rejects_invalid_yaml_syntax(self):
        with pytest.raises(RequirementParseError, match="invalid YAML"):
            parse_requirements_yaml("{ bad yaml {{")


class TestParseRequirementsFile:

    def test_file_not_found(self, tmp_path):
        with pytest.raises(RequirementParseError, match="file not found"):
            parse_requirements_file(tmp_path / "nope.yaml")

    def test_reads_file(self, tmp_path):
        f = tmp_path / "reqs.yaml"
        f.write_text(VALID_YAML, encoding="utf-8")
        result = parse_requirements_file(f)
        assert len(result) == 2


class TestToDomainObjects:

    def test_converts_to_requirements(self):
        parsed = parse_requirements_yaml(VALID_YAML)
        counter = iter(range(100))
        reqs = to_domain_objects(parsed, id_factory=lambda: str(next(counter)))
        assert len(reqs) == 2
        assert reqs[0].slug == "http-client"
        assert reqs[0].id == "0"
        assert len(reqs[0].constraints) == 2
        assert reqs[0].constraints[0].kind == "required_interface"

    def test_default_id_factory(self):
        parsed = parse_requirements_yaml(VALID_YAML)
        reqs = to_domain_objects(parsed)
        assert len(reqs[0].id) == 36  # UUID format


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


def _mk_requirement(conn, project_id: uuid.UUID, slug: str) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO project_requirement (project_id, slug, description) "
            "VALUES (%s, %s, %s) RETURNING id",
            (project_id, slug, f"desc for {slug}"),
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
    conn, req_id: uuid.UUID, cv_id: uuid.UUID,
    verdict: str = "ADOPT",
) -> uuid.UUID:
    profile_id = _mk_rules_profile(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recommendation "
            "(project_requirement_id, rules_profile_id, verdict, reason, "
            " chosen_capability_version_id, pinned_revision_key, "
            " pinned_source_asset_key, rule_name) "
            "VALUES (%s, %s, %s, 'test', %s, 'rev1', 'asset1', 'rule1') "
            "RETURNING id",
            (req_id, profile_id, verdict, cv_id),
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# DB-backed import_requirements tests
# ---------------------------------------------------------------------------

class TestImportRequirements:

    def test_creates_requirements(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        conn.commit()

        result = queries.import_requirements(
            conn, project_id=str(proj), yaml_text=VALID_YAML,
        )
        assert result is not None
        assert result["created"] == 2
        assert result["updated"] == 0
        assert result["total"] == 2
        assert "http-client" in result["requirements"]
        assert "no-urllib3" in result["requirements"]

    def test_upserts_on_duplicate_slug(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        conn.commit()

        queries.import_requirements(
            conn, project_id=str(proj), yaml_text=VALID_YAML,
        )

        updated_yaml = """\
requirements:
  - slug: http-client
    description: "Updated description"
    constraints:
      - kind: required_interface
        name: post
"""
        result = queries.import_requirements(
            conn, project_id=str(proj), yaml_text=updated_yaml,
        )
        assert result["created"] == 0
        assert result["updated"] == 1

        with conn.cursor() as cur:
            cur.execute(
                "SELECT description FROM project_requirement "
                "WHERE project_id = %s AND slug = 'http-client'",
                (proj,),
            )
            assert cur.fetchone()[0] == "Updated description"

            cur.execute(
                "SELECT rc.kind FROM requirement_constraint rc "
                "JOIN project_requirement pr ON pr.id = rc.project_requirement_id "
                "WHERE pr.project_id = %s AND pr.slug = 'http-client'",
                (proj,),
            )
            kinds = [r[0] for r in cur.fetchall()]
            assert kinds == ["required_interface"]

    def test_returns_error_on_bad_yaml(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        conn.commit()

        result = queries.import_requirements(
            conn, project_id=str(proj), yaml_text="requirements: []\n",
        )
        assert result is not None
        assert "error" in result
        assert result["created"] == 0

    def test_returns_none_on_bad_project_id(self, conn):
        result = queries.import_requirements(
            conn, project_id="bad", yaml_text=VALID_YAML,
        )
        assert result is None

    def test_returns_none_on_nonexistent_project(self, conn):
        result = queries.import_requirements(
            conn, project_id=str(uuid.uuid4()), yaml_text=VALID_YAML,
        )
        assert result is None


# ---------------------------------------------------------------------------
# DB-backed requirement_coverage tests
# ---------------------------------------------------------------------------

class TestRequirementCoverage:

    def test_empty_project(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        conn.commit()

        result = queries.requirement_coverage(conn, project_id=str(proj))
        assert result is not None
        assert result["total_requirements"] == 0
        assert result["coverage_pct"] == 0.0

    def test_uncovered_requirements(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        _mk_requirement(conn, proj, "req-a")
        _mk_requirement(conn, proj, "req-b")
        conn.commit()

        result = queries.requirement_coverage(conn, project_id=str(proj))
        assert result["total_requirements"] == 2
        assert result["covered"] == 0
        assert result["coverage_pct"] == 0.0
        assert all(not r["has_recommendation"] for r in result["requirements"])

    def test_covered_requirement(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        cap = _mk_capability(conn, f"pypi:lib-{uuid.uuid4().hex[:6]}", "lib")
        cv = _mk_version(conn, cap)
        req_id = _mk_requirement(conn, proj, "req-covered")
        _mk_recommendation(conn, req_id, cv)
        _mk_requirement(conn, proj, "req-uncovered")
        conn.commit()

        result = queries.requirement_coverage(conn, project_id=str(proj))
        assert result["total_requirements"] == 2
        assert result["covered"] == 1
        assert result["coverage_pct"] == 50.0

        covered = [r for r in result["requirements"] if r["has_recommendation"]]
        assert len(covered) == 1
        assert covered[0]["slug"] == "req-covered"
        assert covered[0]["verdict"] == "ADOPT"

    def test_returns_none_on_bad_project_id(self, conn):
        result = queries.requirement_coverage(conn, project_id="bad")
        assert result is None

    def test_returns_none_on_nonexistent_project(self, conn):
        result = queries.requirement_coverage(
            conn, project_id=str(uuid.uuid4()),
        )
        assert result is None

    def test_coverage_with_verification(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        cap = _mk_capability(conn, f"pypi:vlib-{uuid.uuid4().hex[:6]}", "vlib")
        cv = _mk_version(conn, cap)
        req_id = _mk_requirement(conn, proj, "req-verified")
        _mk_recommendation(conn, req_id, cv)

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO verification_run "
                "(capability_version_id, result) VALUES (%s, 'passed')",
                (cv,),
            )
        conn.commit()

        result = queries.requirement_coverage(conn, project_id=str(proj))
        assert result["verified"] == 1
        verified = [r for r in result["requirements"] if r["has_verification"]]
        assert len(verified) == 1
        assert verified[0]["verification_result"] == "passed"
