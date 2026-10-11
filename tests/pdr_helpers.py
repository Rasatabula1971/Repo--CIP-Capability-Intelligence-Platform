"""
Shared fixtures for the PDR-to-Build tests. Not a test module.

Everything goes through the real tool functions against real Postgres;
the only shortcut is `approve`, which stands in for the human at the
`cip-composer approvals` terminal prompt.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from mcp_server import pdr_approvals
from mcp_server import pdr_queries as q

PDR = """# Clinic Booking PDR

## Goals
Reduce missed calls at the front desk.

## Requirements
1. Staff must sign in with Google OAuth before using the system.
2. The receptionist can see today's bookings on one screen.
3. The app could have dark mode.

## Constraints
All dependencies must be MIT or Apache licensed.

## Glossary
PDR means product design requirements.
"""

REQS: list[dict[str, Any]] = [
    {
        "slug": "staff-sign-in",
        "text": "Staff sign in with Google",
        "priority": "must",
        "acceptance_criteria": [
            "A staff user signs in with a Google account",
            "An unauthenticated request is redirected to sign-in",
        ],
        "quote": "Staff must sign in with Google OAuth before using the system.",
    },
    {
        "slug": "todays-bookings",
        "text": "Receptionist sees today's bookings",
        "priority": "must",
        "acceptance_criteria": ["Today's bookings are listed in time order"],
        "quote": "The receptionist can see today's bookings on one screen.",
        "disposition": "bespoke",
        "disposition_reason": "project-specific screen",
    },
    {
        "slug": "dark-mode",
        "text": "Dark mode",
        "priority": "could",
        "quote": "The app could have dark mode.",
        "disposition": "deferred",
        "disposition_reason": "not in v1",
    },
    {
        "slug": "permissive-licenses",
        "kind": "constraint",
        "text": "MIT or Apache dependencies only",
        "quote": "All dependencies must be MIT or Apache licensed.",
        "disposition": "not_applicable",
        "disposition_reason": "enforced as a license constraint on each need",
    },
]

NEED: dict[str, Any] = {
    "slug": "oauth-login",
    "description": "OAuth2 / OIDC sign-in",
    "search_terms": ["oauth", "oidc login"],
    "problem_types": ["oauth-flow"],
    "satisfies": ["staff-sign-in"],
}

LICENSE_CONSTRAINT = {"kind": "license_allowlist", "spdx_ids": ["MIT", "Apache-2.0"]}

PASS = {
    "outcome": "pass",
    "commit_sha": "abc1234def",
    "test_ref": "tests/test_bookings.py::test_order",
    "command": "pytest tests/test_bookings.py",
    "exit_code": 0,
}


@dataclass
class Ctx:
    pid: str
    sid: str
    reqs: dict[str, str] = field(default_factory=dict)      # slug -> id
    need_id: str | None = None
    cv_id: str | None = None
    lock: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# The human
# ---------------------------------------------------------------------------

def approve(conn, request_id: str, approver: str = "ricky@desk") -> dict[str, Any]:
    """What `cip-composer approvals approve` does once the human confirms."""
    res = pdr_approvals.record_approval(
        conn, request_id=request_id, outcome="approved", approver=approver,
    )
    assert "error" not in res, res
    return res


def reject(conn, request_id: str, note: str = "not this") -> dict[str, Any]:
    res = pdr_approvals.record_approval(
        conn, request_id=request_id, outcome="rejected",
        approver="ricky@desk", note=note,
    )
    assert "error" not in res, res
    return res


# ---------------------------------------------------------------------------
# Registry fixtures
# ---------------------------------------------------------------------------

def make_capability(
    conn,
    name: str = "oauth-library",
    display: str = "OAuth Library",
    *,
    license: str | None = None,
    metadata_license: str | None = None,
    cost_tier: str | None = None,
    intrinsic: float = 0.85,
    bind: bool = True,
    html_url: str | None = None,
    score: bool = True,
) -> tuple[str, str]:
    """
    A searchable capability. With bind=True it has a source binding (so it
    can be pinned and fit-evaluated).

    license          — an ANALYSED component: license evidence exists in
                       evidence_item, which is what the fit matchers read.
    metadata_license — a METADATA-ONLY component, like a GitHub-search
                       ingest: capability.license_spdx is set, but nothing
                       was extracted from its source. Given together with
                       `license`, the registry's metadata says one thing
                       and the source another.
    score            — False leaves the component with no scorecard.
    """
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
            "VALUES (%s, %s) RETURNING id",
            (asset_id, f"rev-{name}"),
        )
        rev_id = cur.fetchone()[0]

        metadata = "{}" if not html_url else '{"html_url": "%s"}' % html_url
        cur.execute(
            "INSERT INTO capability (normalized_key, display_name, ecosystem, kind, "
            "component_kind, license_spdx, cost_tier, metadata) "
            "VALUES (%s, %s, 'pypi', 'library', 'library', %s, %s, %s::jsonb) RETURNING id",
            (f"pypi:{name}", display,
             metadata_license if metadata_license is not None else license,
             cost_tier, metadata),
        )
        cap_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO capability_version (capability_id, version_key, version_kind, "
            "display_version) VALUES (%s, 'content:v1', 'content-hash', '1.0.0') RETURNING id",
            (str(cap_id),),
        )
        cv_id = cur.fetchone()[0]

        if bind:
            cur.execute(
                "INSERT INTO capability_source_binding (capability_version_id, "
                "source_revision_id) VALUES (%s, %s)",
                (str(cv_id), str(rev_id)),
            )
        if license:
            cur.execute(
                "INSERT INTO analysis_run (source_revision_id, extractor_config_version, "
                "status) VALUES (%s, 1, 'completed') RETURNING id",
                (rev_id,),
            )
            run_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO evidence_item (source_revision_id, analysis_run_id, "
                "extractor_name, evidence_type, locator_kind, locator, extracted_value) "
                "VALUES (%s, %s, 'licenses', 'license', 'whole_file', "
                "'{\"path\": \"LICENSE\"}'::jsonb, %s::jsonb)",
                (rev_id, run_id, '{"spdx_id": "%s"}' % license),
            )

        if score:
            cur.execute(
                "INSERT INTO scoring_profile (name, version, profile_hash, dimensions) "
                "VALUES (%s, 1, %s, '{}'::jsonb) RETURNING id",
                (f"prof-{uuid.uuid4().hex[:6]}", uuid.uuid4().hex),
            )
            profile_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO scorecard (capability_version_id, scoring_profile_id, "
                "total_score, confidence, computed_hash) VALUES (%s, %s, %s, 0.9, %s)",
                (str(cv_id), str(profile_id), intrinsic, uuid.uuid4().hex),
            )
    conn.commit()
    return str(cap_id), str(cv_id)


def bind_newer_revision(conn, cv_id: str, key: str = "rev-NEW", *,
                        license: str | None = None) -> str:
    """
    Bind the capability version to a second, more recent source revision.
    That newer revision is the one a decision would pin. By default nobody
    has analysed it; with `license`, its LICENSE file was read and says so.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sr.source_asset_id FROM capability_source_binding csb "
            "JOIN source_revision sr ON sr.id = csb.source_revision_id "
            "WHERE csb.capability_version_id = %s LIMIT 1",
            (cv_id,),
        )
        asset_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO source_revision (source_asset_id, revision_key, identified_at) "
            "VALUES (%s, %s, now() + interval '1 hour') RETURNING id",
            (asset_id, key),
        )
        rev_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO capability_source_binding (capability_version_id, "
            "source_revision_id) VALUES (%s, %s)",
            (cv_id, rev_id),
        )
        if license:
            cur.execute(
                "INSERT INTO analysis_run (source_revision_id, extractor_config_version, "
                "status) VALUES (%s, 1, 'completed') RETURNING id",
                (rev_id,),
            )
            cur.execute(
                "INSERT INTO evidence_item (source_revision_id, analysis_run_id, "
                "extractor_name, evidence_type, locator_kind, locator, extracted_value) "
                "VALUES (%s, %s, 'licenses', 'license', 'whole_file', "
                "'{\"path\": \"LICENSE\"}'::jsonb, %s::jsonb)",
                (rev_id, cur.fetchone()[0], '{"spdx_id": "%s"}' % license),
            )
    conn.commit()
    return key


def _pinned_revision_id(cur, cv_id: str):
    cur.execute(
        "SELECT csb.source_revision_id FROM capability_source_binding csb "
        "JOIN source_revision sr ON sr.id = csb.source_revision_id "
        "WHERE csb.capability_version_id = %s "
        "ORDER BY sr.identified_at DESC, sr.id DESC LIMIT 1",
        (cv_id,),
    )
    return cur.fetchone()[0]


def _evidence(cur, rev_id, extractor: str, kind: str, path: str, value: dict) -> str:
    cur.execute(
        "INSERT INTO analysis_run (source_revision_id, extractor_config_version, status) "
        "VALUES (%s, 1, 'completed') RETURNING id",
        (rev_id,),
    )
    cur.execute(
        "INSERT INTO evidence_item (source_revision_id, analysis_run_id, extractor_name, "
        "evidence_type, locator_kind, locator, extracted_value) "
        "VALUES (%s, %s, %s, %s, 'whole_file', %s::jsonb, %s::jsonb) RETURNING id",
        (rev_id, cur.fetchone()[0], extractor, kind, json.dumps({"path": path}),
         json.dumps(value)),
    )
    return str(cur.fetchone()[0])


def add_interface(conn, cv_id: str, name: str, kind: str = "function") -> None:
    """An interface extracted from the revision that is pinned right now."""
    with conn.cursor() as cur:
        ev = _evidence(cur, _pinned_revision_id(cur, cv_id), "interfaces", "interface",
                       "api.py", {"name": name})
        cur.execute(
            "INSERT INTO capability_interface (capability_version_id, evidence_item_id, "
            "kind, name) VALUES (%s, %s, %s, %s)",
            (cv_id, ev, kind, name),
        )
    conn.commit()


def add_dependency(conn, cv_id: str, ecosystem: str, name: str) -> None:
    """A declared dependency read from the manifest of the pinned revision."""
    with conn.cursor() as cur:
        ev = _evidence(cur, _pinned_revision_id(cur, cv_id), "manifests", "dependency",
                       "pyproject.toml", {"name": name})
        cur.execute(
            "INSERT INTO capability_dependency (capability_version_id, evidence_item_id, "
            "depends_on_ecosystem, depends_on_name, dep_kind) "
            "VALUES (%s, %s, %s, %s, 'runtime')",
            (cv_id, ev, ecosystem, name),
        )
    conn.commit()


def stage_succeeded(conn, cv_id: str, extractor: str) -> None:
    """The pinned revision went through an extractor, whatever it found."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_run (source_revision_id, extractor_config_version, status) "
            "VALUES (%s, 1, 'completed') RETURNING id",
            (_pinned_revision_id(cur, cv_id),),
        )
        cur.execute(
            "INSERT INTO analysis_stage_result (analysis_run_id, extractor_name, status) "
            "VALUES (%s, %s, 'succeeded')",
            (cur.fetchone()[0], extractor),
        )
    conn.commit()


def add_license_evidence(conn, cv_id: str, spdx_id: str, confidence: str | None = None) -> None:
    """License evidence as the extractor writes it, on the pinned revision."""
    value: dict[str, Any] = {"spdx_id": spdx_id}
    if confidence:
        value["confidence"] = confidence
    with conn.cursor() as cur:
        _evidence(cur, _pinned_revision_id(cur, cv_id), "license", "license", "LICENSE", value)
    conn.commit()


# ---------------------------------------------------------------------------
# Workflow stages
# ---------------------------------------------------------------------------

def ingest(conn, name: str = "test-project", pdr: str = PDR) -> dict[str, Any]:
    return q.ingest_pdr(conn, project_name=name, pdr_title="Test PDR", pdr_text=pdr)


def record_reqs(conn, pid: str, sid: str, reqs: list[dict] | None = None) -> dict[str, Any]:
    return q.record_requirements(
        conn, project_id=pid, requirement_source_id=sid,
        requirements=reqs if reqs is not None else REQS,
    )


def with_requirements(conn, name: str = "test-project") -> Ctx:
    ing = ingest(conn, name)
    rr = record_reqs(conn, ing["project_id"], ing["requirement_source_id"])
    assert all(r["valid"] for r in rr["requirements"]), rr
    return Ctx(
        pid=ing["project_id"], sid=ing["requirement_source_id"],
        reqs={r["slug"]: r["req_id"] for r in rr["requirements"]},
    )


def record_need(conn, pid: str, **overrides: Any) -> dict[str, Any]:
    res = q.record_capability_needs(conn, project_id=pid, needs=[{**NEED, **overrides}])
    assert res["needs"][0]["valid"], res
    return res["needs"][0]


def confirm(conn, pid: str) -> dict[str, Any]:
    rc = q.request_requirements_confirmation(conn, project_id=pid)
    assert rc["status"] == "awaiting_approval", rc
    approve(conn, rc["approval_request_id"])
    return rc


def scoped(conn, *, constraints: list[dict] | None = None, name: str = "test-project") -> Ctx:
    """PDR ingested, requirements + one capability need recorded, scope confirmed."""
    ctx = with_requirements(conn, name)
    need = record_need(conn, ctx.pid, **({"constraints": constraints} if constraints else {}))
    ctx.need_id = need["need_id"]
    confirm(conn, ctx.pid)
    return ctx


def decided_build(conn, name: str = "test-project") -> Ctx:
    """Scoped, swept against an empty registry, decided BUILD (cold start)."""
    ctx = scoped(conn, name=name)
    assert q.search_for_requirement(conn, req_id=ctx.need_id)["candidate_count"] == 0
    d = q.record_decision(conn, req_id=ctx.need_id)
    assert d["verdict"] == "BUILD", d
    return ctx


def decided_adopt(conn, name: str = "test-project") -> Ctx:
    """
    Scoped with a license constraint, one MIT candidate in the registry,
    decided by CIP's rules as ADOPT.
    """
    _, cv_id = make_capability(conn, license="MIT")
    ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT], name=name)
    ctx.cv_id = cv_id
    q.search_for_requirement(conn, req_id=ctx.need_id)
    d = q.record_decision(conn, req_id=ctx.need_id)
    assert d["verdict"] == "ADOPT" and d["decided_by"] == "cip_rules", d
    return ctx


def lock(conn, pid: str, **inputs: Any) -> dict[str, Any]:
    """
    Both lock calls with the human approval in between. The second call
    passes only the approved request's id, which is how a session that has
    lost the original inputs completes a lock.
    """
    first = q.lock_architecture(conn, project_id=pid, **inputs)
    assert first.get("status") == "awaiting_approval", first
    approve(conn, first["approval_request_id"])
    second = q.lock_architecture(
        conn, project_id=pid, approval_request_id=first["approval_request_id"],
    )
    assert "architecture_lock_id" in second, second
    return second


def locked_build(conn, name: str = "test-project") -> Ctx:
    ctx = decided_build(conn, name)
    ctx.lock = lock(conn, ctx.pid)
    return ctx


def locked_adopt(conn, name: str = "test-project") -> Ctx:
    ctx = decided_adopt(conn, name)
    ctx.lock = lock(conn, ctx.pid)
    return ctx


def bespoke_task(conn, ctx: Ctx, **overrides: Any) -> dict[str, Any]:
    """A task on the bespoke requirement — needs no capability."""
    args = dict(
        project_id=ctx.pid, title="Bookings screen", objective="List today's bookings",
        req_ids=["todays-bookings"],
        permitted_paths=["src/bookings/", "tests/test_bookings.py"],
    )
    args.update(overrides)
    return q.record_build_progress(conn, **args)


def scalar(conn, sql: str, params: tuple = ()) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row[0] if row else None


def add_unlinked_evidence(conn, cv_id: str, evidence_type: str, value: dict[str, Any]) -> str:
    """
    Evidence on the pinned revision that promotion never linked to the
    version: what a component looks like when it was ingested from registry
    metadata (which creates the binding) and analysed afterwards (promotion
    is then a no-op). Returns the evidence item id.
    """
    extractor = {"interface": "interfaces", "dependency": "manifests"}.get(
        evidence_type, evidence_type)
    with conn.cursor() as cur:
        ev = _evidence(cur, _pinned_revision_id(cur, cv_id), extractor, evidence_type,
                       "src", value)
    conn.commit()
    return ev
