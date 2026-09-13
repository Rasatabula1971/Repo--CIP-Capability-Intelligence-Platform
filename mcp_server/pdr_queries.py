"""
PDR-to-Build workflow queries.

Nine tool implementations for the vNext spec:
  1. ingest_pdr          — register project + store PDR provenance
  2. record_requirements — validate + assign REQ-IDs
  3. search_for_requirement — registry search + fit for one req
  4. record_decision     — record verdict (enforces search-before-build)
  5. build_approval_brief — render approval brief (view)
  6. lock_architecture   — freeze decisions into versioned lock
  7. check_against_lock  — test proposed change against lock (query)
  8. record_build_progress — record tasks + link to requirements
  9. coverage            — report PDR progress (query)
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from typing import Any, Optional

import psycopg

from core.project.fit import evaluate_fit
from core.project.types import (
    CandidateInputs,
    Constraint,
    DependencyEvidence,
    FitResult,
    InterfaceEvidence,
    LicenseEvidence,
    Requirement,
)
from mcp_server.queries import search_capabilities

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_uuid(s: str) -> bool:
    try:
        uuid.UUID(s)
        return True
    except (ValueError, AttributeError):
        return False


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _slugify(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")[:80]


# ---------------------------------------------------------------------------
# Candidate loading + fit persistence (shared with search_for_requirement)
# ---------------------------------------------------------------------------

def _load_candidate_inputs(
    conn: psycopg.Connection, cv_id: str,
) -> CandidateInputs | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(total_score), 0.0) FROM scorecard "
            "WHERE capability_version_id = %s",
            (cv_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        intrinsic_score = float(row[0])

        cur.execute(
            """
            SELECT sr.revision_key, sa.external_key
            FROM capability_source_binding csb
            JOIN source_revision sr ON sr.id = csb.source_revision_id
            JOIN source_asset sa    ON sa.id = sr.source_asset_id
            WHERE csb.capability_version_id = %s
            ORDER BY sr.identified_at DESC
            LIMIT 1
            """,
            (cv_id,),
        )
        binding = cur.fetchone()
        if not binding:
            return None
        pinned_revision_key, pinned_source_asset_key = binding

        cur.execute(
            """
            SELECT evidence_item_id, kind, name,
                   COALESCE(signature, ''), language
            FROM capability_interface
            WHERE capability_version_id = %s
            ORDER BY name
            """,
            (cv_id,),
        )
        interfaces = tuple(
            InterfaceEvidence(
                evidence_item_id=str(r[0]),
                kind=r[1], name=r[2], signature=r[3], language=r[4],
            )
            for r in cur.fetchall()
        )

        cur.execute(
            """
            SELECT evidence_item_id, depends_on_ecosystem,
                   depends_on_name, dep_kind
            FROM capability_dependency
            WHERE capability_version_id = %s
            ORDER BY depends_on_ecosystem, depends_on_name
            """,
            (cv_id,),
        )
        deps = tuple(
            DependencyEvidence(
                evidence_item_id=str(r[0]),
                ecosystem=r[1], name=r[2], kind=r[3],
            )
            for r in cur.fetchall()
        )

        cur.execute(
            """
            SELECT ei.id, ei.extracted_value
            FROM evidence_item ei
            JOIN capability_source_binding csb
              ON csb.source_revision_id = ei.source_revision_id
            WHERE csb.capability_version_id = %s
              AND ei.evidence_type = 'license'
            ORDER BY ei.id
            """,
            (cv_id,),
        )
        licenses = tuple(
            LicenseEvidence(
                evidence_item_id=str(r[0]),
                spdx_id=r[1].get("spdx_id", "unknown") if r[1] else "unknown",
            )
            for r in cur.fetchall()
        )

    return CandidateInputs(
        capability_version_id=cv_id,
        intrinsic_score=intrinsic_score,
        interfaces=interfaces,
        dependencies=deps,
        licenses=licenses,
        pinned_revision_key=pinned_revision_key,
        pinned_source_asset_key=pinned_source_asset_key,
    )


def _persist_fit_evaluation(
    conn: psycopg.Connection,
    *,
    project_requirement_id: str,
    candidate: CandidateInputs,
    fit_result: FitResult,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fit_evaluation
              (project_requirement_id, capability_version_id,
               fit_score, blocking_gap_count, computed_hash)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (project_requirement_id, capability_version_id)
              DO UPDATE SET fit_score = EXCLUDED.fit_score,
                            blocking_gap_count = EXCLUDED.blocking_gap_count,
                            computed_hash = EXCLUDED.computed_hash,
                            computed_at = now()
            RETURNING id
            """,
            (
                project_requirement_id,
                candidate.capability_version_id,
                fit_result.fit_score,
                fit_result.blocking_gap_count,
                fit_result.computed_hash,
            ),
        )
        fit_id = str(cur.fetchone()[0])

        cur.execute("DELETE FROM fit_gap WHERE fit_evaluation_id = %s", (fit_id,))
        cur.execute("DELETE FROM fit_evidence_link WHERE fit_evaluation_id = %s", (fit_id,))

        for gap in fit_result.gaps:
            cur.execute(
                """
                INSERT INTO fit_gap (fit_evaluation_id, kind, is_blocking, detail)
                VALUES (%s, %s, %s, %s::jsonb)
                """,
                (fit_id, gap.kind, gap.is_blocking, json.dumps(gap.detail)),
            )
        for link in fit_result.evidence_links:
            cur.execute(
                """
                INSERT INTO fit_evidence_link (fit_evaluation_id, evidence_item_id, role)
                VALUES (%s, %s, %s)
                """,
                (fit_id, link.evidence_item_id, link.role),
            )

    return fit_id


def _read_fit_evaluations(
    conn: psycopg.Connection, req_id: str,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fe.id, fe.capability_version_id, fe.fit_score,
                   fe.blocking_gap_count, fe.computed_hash,
                   cv.display_version,
                   c.normalized_key, c.display_name, c.ecosystem,
                   c.component_kind, c.runtime, c.cost_tier,
                   cs.total_score
            FROM fit_evaluation fe
            JOIN capability_version cv ON cv.id = fe.capability_version_id
            JOIN capability c ON c.id = cv.capability_id
            LEFT JOIN component_score cs ON cs.capability_id = c.id
            WHERE fe.project_requirement_id = %s
            ORDER BY fe.fit_score DESC, cs.total_score DESC NULLS LAST
            """,
            (req_id,),
        )
        candidates = []
        for r in cur.fetchall():
            candidates.append({
                "fit_evaluation_id": str(r[0]),
                "capability_version_id": str(r[1]),
                "fit_score": float(r[2]),
                "blocking_gap_count": r[3],
                "display_version": r[5],
                "normalized_key": r[6],
                "display_name": r[7],
                "ecosystem": r[8],
                "component_kind": r[9],
                "runtime": r[10],
                "cost_tier": r[11],
                "intrinsic_score": float(r[12]) if r[12] else None,
            })
    return candidates


# ---------------------------------------------------------------------------
# 1. ingest_pdr
# ---------------------------------------------------------------------------

def ingest_pdr(
    conn: psycopg.Connection,
    *,
    project_name: str,
    pdr_title: str,
    pdr_text: str,
    project_id: str | None = None,
    stack: str | None = None,
    policy_profile: str | None = None,
) -> dict[str, Any]:
    content_hash = _sha256(pdr_text)

    with conn.cursor() as cur:
        if project_id and _is_uuid(project_id):
            cur.execute("SELECT id FROM project WHERE id = %s", (project_id,))
            row = cur.fetchone()
            if not row:
                return {"error": f"Project {project_id} not found"}
            pid = row[0]
        else:
            cur.execute(
                """
                INSERT INTO project (name, metadata)
                VALUES (%s, %s::jsonb)
                ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
                RETURNING id
                """,
                (project_name, json.dumps({
                    "stack": stack,
                    "policy_profile": policy_profile,
                })),
            )
            pid = cur.fetchone()[0]

        cur.execute(
            """
            SELECT id FROM requirement_source
            WHERE project_id = %s AND content_hash = %s
            """,
            (str(pid), content_hash),
        )
        existing = cur.fetchone()
        if existing:
            conn.commit()
            return {
                "project_id": str(pid),
                "requirement_source_id": str(existing[0]),
                "content_hash": content_hash,
                "was_duplicate": True,
            }

        cur.execute(
            """
            INSERT INTO requirement_source (project_id, title, doc_kind, content_hash)
            VALUES (%s, %s, 'pdr', %s)
            RETURNING id
            """,
            (str(pid), pdr_title, content_hash),
        )
        rs_id = cur.fetchone()[0]

    conn.commit()
    return {
        "project_id": str(pid),
        "requirement_source_id": str(rs_id),
        "content_hash": content_hash,
        "was_duplicate": False,
    }


# ---------------------------------------------------------------------------
# 2. record_requirements
# ---------------------------------------------------------------------------

_VALID_PRIORITIES = {"must", "should", "could", "wont"}


def _validate_requirement(req: dict) -> list[str]:
    errors = []
    if not req.get("text", "").strip():
        errors.append("text is required")
    if not req.get("acceptance_criteria", "").strip():
        errors.append("acceptance_criteria is required")
    pri = req.get("priority", "must")
    if pri not in _VALID_PRIORITIES:
        errors.append(f"priority must be one of {sorted(_VALID_PRIORITIES)}, got '{pri}'")
    if not req.get("source_span"):
        errors.append("source_span is required for traceability")
    return errors


def record_requirements(
    conn: psycopg.Connection,
    *,
    project_id: str,
    requirement_source_id: str,
    requirements: list[dict[str, Any]],
) -> dict[str, Any]:
    if not _is_uuid(project_id) or not _is_uuid(requirement_source_id):
        return {"error": "Invalid UUID"}

    with conn.cursor() as cur:
        cur.execute("SELECT id FROM project WHERE id = %s", (project_id,))
        if not cur.fetchone():
            return {"error": f"Project {project_id} not found"}

        cur.execute(
            "SELECT id FROM requirement_source WHERE id = %s AND project_id = %s",
            (requirement_source_id, project_id),
        )
        if not cur.fetchone():
            return {"error": f"Requirement source {requirement_source_id} not found"}

    results = []
    for i, req in enumerate(requirements):
        validation_errors = _validate_requirement(req)
        if validation_errors:
            results.append({
                "index": i,
                "valid": False,
                "errors": validation_errors,
            })
            continue

        text = req["text"].strip()
        slug = req.get("slug") or _slugify(text)
        pri = req.get("priority", "must")
        ac = req["acceptance_criteria"].strip()
        source_span = req.get("source_span")
        constraints = req.get("constraints", [])

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO project_requirement
                    (project_id, slug, description, requirement_source_id,
                     source_span, priority, acceptance_criteria)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (project_id, slug) DO UPDATE SET
                    description = EXCLUDED.description,
                    requirement_source_id = EXCLUDED.requirement_source_id,
                    source_span = EXCLUDED.source_span,
                    priority = EXCLUDED.priority,
                    acceptance_criteria = EXCLUDED.acceptance_criteria
                RETURNING id, slug
                """,
                (
                    project_id, slug, text, requirement_source_id,
                    json.dumps(source_span) if source_span else None,
                    pri, ac,
                ),
            )
            row = cur.fetchone()
            req_id = str(row[0])
            req_slug = row[1]

            for c in constraints:
                cur.execute(
                    """
                    INSERT INTO requirement_constraint
                        (project_requirement_id, kind, detail)
                    VALUES (%s, %s, %s::jsonb)
                    """,
                    (req_id, c.get("kind", "general"), json.dumps(c)),
                )

        results.append({
            "index": i,
            "req_id": req_id,
            "slug": req_slug,
            "valid": True,
            "errors": [],
        })

    conn.commit()
    return {
        "project_id": project_id,
        "requirement_source_id": requirement_source_id,
        "requirements": results,
    }


# ---------------------------------------------------------------------------
# 3. search_for_requirement
# ---------------------------------------------------------------------------

def search_for_requirement(
    conn: psycopg.Connection,
    *,
    req_id: str,
    search_query: str | None = None,
    ecosystem: str | None = None,
    capability_kind: str | None = None,
    component_kind: str | None = None,
    runtime: str | None = None,
    cost_tier: str | None = None,
    limit: int = 20,
) -> dict[str, Any] | None:
    if not _is_uuid(req_id):
        return None

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pr.id, pr.slug, pr.description, pr.project_id
            FROM project_requirement pr
            WHERE pr.id = %s
            """,
            (req_id,),
        )
        req_row = cur.fetchone()
        if not req_row:
            return None

        cur.execute(
            "SELECT kind, detail FROM requirement_constraint WHERE project_requirement_id = %s",
            (req_id,),
        )
        constraint_rows = cur.fetchall()

    constraints = [{"kind": r[0], "detail": r[1]} for r in constraint_rows]
    project_id = str(req_row[3])

    query_text = (search_query or req_row[2] or "").strip()
    if query_text:
        search_results = search_capabilities(
            conn, query_text,
            ecosystem=ecosystem,
            capability_kind=capability_kind,
            component_kind=component_kind,
            runtime=runtime,
            cost_tier=cost_tier,
            project_id=project_id,
            limit=limit,
        )

        requirement = Requirement(
            id=req_id,
            slug=req_row[1],
            description=req_row[2] or "",
            constraints=tuple(
                Constraint(kind=r[0], detail=r[1]) for r in constraint_rows
            ),
        )

        for sr in search_results:
            cv_id = sr.get("head_version_id")
            if not cv_id:
                continue
            inputs = _load_candidate_inputs(conn, cv_id)
            if inputs is None:
                continue
            fit_result = evaluate_fit(requirement, inputs)
            _persist_fit_evaluation(
                conn,
                project_requirement_id=req_id,
                candidate=inputs,
                fit_result=fit_result,
            )
        conn.commit()

    candidates = _read_fit_evaluations(conn, req_id)

    return {
        "req_id": req_id,
        "slug": req_row[1],
        "description": req_row[2],
        "project_id": project_id,
        "constraints": constraints,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "search_query": query_text or None,
    }


# ---------------------------------------------------------------------------
# 4. record_decision
# ---------------------------------------------------------------------------

_VALID_VERDICTS = {"ADOPT", "ADAPT", "WRAP", "REFERENCE", "REJECT", "BUILD"}
_REUSE_VERDICTS = {"ADOPT", "ADAPT", "WRAP", "REFERENCE"}


def record_decision(
    conn: psycopg.Connection,
    *,
    req_id: str,
    verdict: str,
    chosen_capability_version_id: str | None = None,
    evidence_refs: list[str] | None = None,
    rationale: str = "",
) -> dict[str, Any] | None:
    if not _is_uuid(req_id):
        return None
    if verdict not in _VALID_VERDICTS:
        return {"refused": True, "reason": f"Invalid verdict: {verdict}"}

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, project_id FROM project_requirement WHERE id = %s",
            (req_id,),
        )
        req_row = cur.fetchone()
        if not req_row:
            return None

        if verdict == "BUILD":
            cur.execute(
                "SELECT id FROM fit_evaluation WHERE project_requirement_id = %s LIMIT 1",
                (req_id,),
            )
            if not cur.fetchone():
                return {
                    "refused": True,
                    "reason": "BUILD verdict requires a prior search (search_for_requirement). "
                              "No fit_evaluation rows exist for this requirement.",
                }

        if verdict in _REUSE_VERDICTS:
            if not chosen_capability_version_id or not _is_uuid(chosen_capability_version_id):
                return {
                    "refused": True,
                    "reason": f"{verdict} verdict requires a chosen_capability_version_id.",
                }
            cur.execute(
                "SELECT id FROM capability_version WHERE id = %s",
                (chosen_capability_version_id,),
            )
            if not cur.fetchone():
                return {
                    "refused": True,
                    "reason": f"capability_version {chosen_capability_version_id} not found.",
                }

        cur.execute(
            """
            SELECT id FROM recommendation_rules_profile
            ORDER BY created_at DESC LIMIT 1
            """,
        )
        profile_row = cur.fetchone()
        if not profile_row:
            cur.execute(
                """
                INSERT INTO recommendation_rules_profile (name, version, profile_hash, rules)
                VALUES ('pdr_workflow', 1, %s, %s::jsonb)
                RETURNING id
                """,
                (_sha256("pdr_workflow_v1"), json.dumps({"source": "pdr_workflow"})),
            )
            profile_row = cur.fetchone()
        rules_profile_id = profile_row[0]

        cur.execute(
            "DELETE FROM recommendation WHERE project_requirement_id = %s",
            (req_id,),
        )

        pinned_revision = None
        pinned_asset = None
        if chosen_capability_version_id and verdict in _REUSE_VERDICTS:
            cur.execute(
                """
                SELECT sr.revision_key, sa.external_key
                FROM capability_source_binding csb
                JOIN source_revision sr ON sr.id = csb.source_revision_id
                JOIN source_asset sa ON sa.id = sr.source_asset_id
                WHERE csb.capability_version_id = %s
                LIMIT 1
                """,
                (chosen_capability_version_id,),
            )
            pin_row = cur.fetchone()
            if pin_row:
                pinned_revision = pin_row[0]
                pinned_asset = pin_row[1]
            else:
                pinned_revision = "unbound"
                pinned_asset = "unbound"

        cur.execute(
            """
            INSERT INTO recommendation
                (project_requirement_id, rules_profile_id, verdict,
                 chosen_capability_version_id, pinned_revision_key,
                 pinned_source_asset_key, rule_name, reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                req_id, str(rules_profile_id), verdict,
                chosen_capability_version_id,
                pinned_revision, pinned_asset,
                "pdr_decision", rationale or f"{verdict} per PDR workflow",
            ),
        )
        rec_id = str(cur.fetchone()[0])

    conn.commit()
    return {"recommendation_id": rec_id, "verdict": verdict}


# ---------------------------------------------------------------------------
# 5. build_approval_brief
# ---------------------------------------------------------------------------

_HUMAN_APPROVAL_KEYWORDS = {
    "paid", "recurring", "vendor", "closed-source", "proprietary",
    "sensitive", "external", "ai provider", "auth provider", "payment",
    "lock-in", "security", "compliance",
}


def build_approval_brief(
    conn: psycopg.Connection,
    *,
    project_id: str,
) -> dict[str, Any] | None:
    if not _is_uuid(project_id):
        return None

    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM project WHERE id = %s", (project_id,))
        proj = cur.fetchone()
        if not proj:
            return None

        cur.execute(
            """
            SELECT pr.id, pr.slug, pr.description, pr.priority,
                   pr.acceptance_criteria,
                   r.id AS rec_id, r.verdict, r.reason,
                   r.chosen_capability_version_id,
                   c.normalized_key, c.cost_tier, c.license_spdx,
                   c.runtime
            FROM project_requirement pr
            LEFT JOIN recommendation r ON r.project_requirement_id = pr.id
            LEFT JOIN capability_version cv ON cv.id = r.chosen_capability_version_id
            LEFT JOIN capability c ON c.id = cv.capability_id
            WHERE pr.project_id = %s
            ORDER BY pr.slug
            """,
            (project_id,),
        )
        rows = cur.fetchall()

    decisions = []
    human_approval_items = []
    unresolved = []

    for row in rows:
        req_id, slug, desc, priority, ac, rec_id, verdict, reason, cv_id, \
            cap_key, cost_tier, license_spdx, runtime = row

        entry = {
            "req_id": str(req_id),
            "slug": slug,
            "description": desc,
            "priority": priority,
            "verdict": verdict,
            "reason": reason,
            "capability": cap_key,
            "cost_tier": cost_tier,
            "license": license_spdx,
            "runtime": runtime,
        }
        decisions.append(entry)

        if not verdict:
            unresolved.append({"req_id": str(req_id), "slug": slug, "reason": "No decision recorded"})
            continue

        needs_human = False
        desc_lower = (desc or "").lower() + " " + (reason or "").lower()
        for kw in _HUMAN_APPROVAL_KEYWORDS:
            if kw in desc_lower:
                needs_human = True
                break
        if cost_tier and cost_tier not in ("free", "free_tier"):
            needs_human = True
        if license_spdx and license_spdx not in (
            "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD", "Unlicense",
        ):
            needs_human = True

        if needs_human:
            human_approval_items.append(entry)

    lines = [f"# Approval Brief — {proj[1]}", ""]
    for d in decisions:
        v = d["verdict"] or "UNDECIDED"
        cap = f" → {d['capability']}" if d["capability"] else ""
        lines.append(f"- **{d['slug']}** [{v}]{cap}")
        if d["reason"]:
            lines.append(f"  Rationale: {d['reason']}")
    if human_approval_items:
        lines.append("")
        lines.append("## Items requiring human approval")
        for h in human_approval_items:
            lines.append(f"- {h['slug']}: {h['verdict']} — cost={h['cost_tier']}, license={h['license']}")
    if unresolved:
        lines.append("")
        lines.append("## Unresolved requirements")
        for u in unresolved:
            lines.append(f"- {u['slug']}: {u['reason']}")

    return {
        "brief_markdown": "\n".join(lines),
        "decisions": decisions,
        "human_approval_items": human_approval_items,
        "unresolved": unresolved,
    }


# ---------------------------------------------------------------------------
# 6. lock_architecture
# ---------------------------------------------------------------------------

def lock_architecture(
    conn: psycopg.Connection,
    *,
    project_id: str,
    approved_by: str,
    approval_note: str | None = None,
    unresolved_risks: list[str] | None = None,
) -> dict[str, Any] | None:
    if not _is_uuid(project_id):
        return None

    with conn.cursor() as cur:
        cur.execute("SELECT id FROM project WHERE id = %s", (project_id,))
        if not cur.fetchone():
            return None

        cur.execute(
            """
            SELECT pr.id, pr.slug
            FROM project_requirement pr
            WHERE pr.project_id = %s
              AND NOT EXISTS (
                  SELECT 1 FROM recommendation r
                  WHERE r.project_requirement_id = pr.id
              )
            """,
            (project_id,),
        )
        undecided = cur.fetchall()
        if undecided:
            slugs = [r[1] for r in undecided]
            return {
                "refused": True,
                "reason": f"Requirements without decisions: {', '.join(slugs)}",
            }

        cur.execute(
            """
            SELECT r.id, pr.slug, r.verdict, r.chosen_capability_version_id,
                   r.pinned_revision_key
            FROM recommendation r
            JOIN project_requirement pr ON pr.id = r.project_requirement_id
            WHERE pr.project_id = %s
            ORDER BY pr.slug
            """,
            (project_id,),
        )
        rec_rows = cur.fetchall()
        decisions_list = []
        for r in rec_rows:
            decisions_list.append({
                "recommendation_id": str(r[0]),
                "slug": r[1],
                "verdict": r[2],
                "capability_version_id": str(r[3]) if r[3] else None,
                "pinned_revision": r[4],
            })

        decisions_json = json.dumps(decisions_list, sort_keys=True)
        source_lock_hash = _sha256(decisions_json)

        cur.execute(
            """
            UPDATE architecture_lock
            SET status = 'superseded'
            WHERE project_id = %s AND status = 'active'
            """,
            (project_id,),
        )

        cur.execute(
            """
            SELECT COALESCE(MAX(version), 0) FROM architecture_lock
            WHERE project_id = %s
            """,
            (project_id,),
        )
        next_version = cur.fetchone()[0] + 1

        cur.execute(
            """
            INSERT INTO architecture_lock
                (project_id, version, status, approved_by, approval_note,
                 decisions, unresolved_risks, source_lock_hash)
            VALUES (%s, %s, 'active', %s, %s, %s::jsonb, %s::jsonb, %s)
            RETURNING id
            """,
            (
                project_id, next_version, approved_by, approval_note,
                decisions_json,
                json.dumps(unresolved_risks or []),
                source_lock_hash,
            ),
        )
        lock_id = str(cur.fetchone()[0])

    conn.commit()
    return {
        "architecture_lock_id": lock_id,
        "version": next_version,
        "source_lock_hash": source_lock_hash,
        "decision_count": len(decisions_list),
    }


# ---------------------------------------------------------------------------
# 7. check_against_lock
# ---------------------------------------------------------------------------

def check_against_lock(
    conn: psycopg.Connection,
    *,
    project_id: str,
    proposed_change: dict[str, Any],
) -> dict[str, Any] | None:
    if not _is_uuid(project_id):
        return None

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, decisions FROM architecture_lock
            WHERE project_id = %s AND status = 'active'
            """,
            (project_id,),
        )
        lock_row = cur.fetchone()
        if not lock_row:
            return {"verdict": "allowed", "reason": "No active architecture lock exists."}

        decisions = lock_row[1] if isinstance(lock_row[1], list) else json.loads(lock_row[1])

    locked_by_slug = {d["slug"]: d for d in decisions}
    locked_cv_ids = {d["capability_version_id"] for d in decisions if d.get("capability_version_id")}

    change_req_ids = proposed_change.get("req_ids", [])
    change_provider = proposed_change.get("provider")
    change_cv_id = proposed_change.get("capability_version_id")
    change_desc = proposed_change.get("description", "")

    conflicts = []

    if change_cv_id and change_cv_id not in locked_cv_ids:
        for d in decisions:
            if d.get("capability_version_id") and d["verdict"] in ("ADOPT", "ADAPT", "WRAP"):
                conflicts.append(
                    f"Locked decision for '{d['slug']}' pins capability_version "
                    f"{d['capability_version_id']}; proposed change uses {change_cv_id}"
                )

    if change_provider:
        for d in decisions:
            desc_lower = change_desc.lower()
            if change_provider.lower() in (d.get("slug", "") + " " + d.get("verdict", "")).lower():
                conflicts.append(
                    f"Provider '{change_provider}' touches locked decision for '{d['slug']}'"
                )

    if conflicts:
        return {
            "verdict": "needs_approval",
            "reason": "; ".join(conflicts),
            "conflicts": conflicts,
        }

    return {"verdict": "allowed", "reason": "Proposed change does not conflict with locked decisions."}


# ---------------------------------------------------------------------------
# 8. record_build_progress
# ---------------------------------------------------------------------------

def record_build_progress(
    conn: psycopg.Connection,
    *,
    # create mode
    project_id: str | None = None,
    architecture_lock_id: str | None = None,
    title: str | None = None,
    objective: str | None = None,
    req_ids: list[str] | None = None,
    permitted_paths: list[str] | None = None,
    prohibited_changes: list[str] | None = None,
    reused_capability_version_id: str | None = None,
    # update mode
    build_task_id: str | None = None,
    status: str | None = None,
    target_commit: str | None = None,
    verification: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if build_task_id and _is_uuid(build_task_id):
        return _update_build_task(
            conn, build_task_id=build_task_id,
            status=status, target_commit=target_commit,
            verification=verification,
        )
    else:
        return _create_build_task(
            conn,
            project_id=project_id,
            architecture_lock_id=architecture_lock_id,
            title=title, objective=objective,
            req_ids=req_ids or [],
            permitted_paths=permitted_paths,
            prohibited_changes=prohibited_changes,
            reused_capability_version_id=reused_capability_version_id,
        )


def _create_build_task(
    conn, *, project_id, architecture_lock_id, title, objective,
    req_ids, permitted_paths, prohibited_changes, reused_capability_version_id,
) -> dict[str, Any] | None:
    if not project_id or not _is_uuid(project_id):
        return {"error": "project_id is required"}
    if not architecture_lock_id or not _is_uuid(architecture_lock_id):
        return {"error": "architecture_lock_id is required"}
    if not title or not objective:
        return {"error": "title and objective are required"}
    if not req_ids:
        return {"error": "At least one req_id is required"}

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM architecture_lock WHERE id = %s AND project_id = %s",
            (architecture_lock_id, project_id),
        )
        if not cur.fetchone():
            return {"error": "Architecture lock not found for this project"}

        reused_cv = None
        if reused_capability_version_id and _is_uuid(reused_capability_version_id):
            reused_cv = reused_capability_version_id

        cur.execute(
            """
            INSERT INTO build_task
                (project_id, architecture_lock_id, title, objective,
                 permitted_paths, prohibited_changes, reused_capability_version_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                project_id, architecture_lock_id, title, objective,
                permitted_paths, prohibited_changes, reused_cv,
            ),
        )
        task_id = str(cur.fetchone()[0])

        for rid in req_ids:
            if _is_uuid(rid):
                cur.execute(
                    """
                    INSERT INTO build_task_requirement (build_task_id, project_requirement_id)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (task_id, rid),
                )

    conn.commit()
    return {"build_task_id": task_id, "status": "planned"}


def _update_build_task(
    conn, *, build_task_id, status, target_commit, verification,
) -> dict[str, Any] | None:
    _VALID_STATUSES = {"planned", "in_progress", "implemented", "verified", "abandoned"}

    with conn.cursor() as cur:
        cur.execute("SELECT id, status FROM build_task WHERE id = %s", (build_task_id,))
        row = cur.fetchone()
        if not row:
            return {"error": f"Build task {build_task_id} not found"}

        updates = []
        params = []
        if status and status in _VALID_STATUSES:
            updates.append("status = %s")
            params.append(status)
        if target_commit:
            updates.append("target_commit = %s")
            params.append(target_commit)
        if updates:
            updates.append("updated_at = now()")
            params.append(build_task_id)
            cur.execute(
                f"UPDATE build_task SET {', '.join(updates)} WHERE id = %s",
                params,
            )

        if verification:
            req_id = verification.get("req_id")
            outcome = verification.get("outcome", "not_run")
            evidence_ref = verification.get("evidence_ref")
            if req_id and _is_uuid(req_id):
                cur.execute(
                    """
                    INSERT INTO requirement_verification
                        (project_requirement_id, build_task_id, outcome, evidence_ref)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                    """,
                    (req_id, build_task_id, outcome, evidence_ref),
                )

    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM build_task WHERE id = %s", (build_task_id,))
        final_status = cur.fetchone()[0]

    return {"build_task_id": build_task_id, "status": final_status}


# ---------------------------------------------------------------------------
# 9. coverage
# ---------------------------------------------------------------------------

def coverage(
    conn: psycopg.Connection,
    *,
    project_id: str,
) -> dict[str, Any] | None:
    if not _is_uuid(project_id):
        return None

    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM project WHERE id = %s", (project_id,))
        proj = cur.fetchone()
        if not proj:
            return None

        cur.execute(
            """
            SELECT pr.id, pr.slug, pr.priority,
                   r.verdict,
                   bt.id AS task_id, bt.status AS task_status,
                   rv.outcome AS verification_outcome
            FROM project_requirement pr
            LEFT JOIN recommendation r ON r.project_requirement_id = pr.id
            LEFT JOIN build_task_requirement btr ON btr.project_requirement_id = pr.id
            LEFT JOIN build_task bt ON bt.id = btr.build_task_id
            LEFT JOIN LATERAL (
                SELECT outcome FROM requirement_verification
                WHERE project_requirement_id = pr.id
                ORDER BY verified_at DESC LIMIT 1
            ) rv ON true
            WHERE pr.project_id = %s
            ORDER BY pr.slug
            """,
            (project_id,),
        )
        rows = cur.fetchall()

    total = 0
    decided = 0
    tasked = 0
    implemented = 0
    verified = 0
    by_requirement = []
    seen_reqs = set()

    for row in rows:
        req_id, slug, priority, verdict, task_id, task_status, ver_outcome = row
        req_key = str(req_id)
        if req_key in seen_reqs:
            continue
        seen_reqs.add(req_key)
        total += 1
        is_decided = verdict is not None
        is_tasked = task_id is not None
        is_implemented = task_status in ("implemented", "verified")
        is_verified = ver_outcome == "pass"

        if is_decided:
            decided += 1
        if is_tasked:
            tasked += 1
        if is_implemented:
            implemented += 1
        if is_verified:
            verified += 1

        by_requirement.append({
            "req_id": req_key,
            "slug": slug,
            "priority": priority,
            "verdict": verdict,
            "task_status": task_status,
            "verification_outcome": ver_outcome,
        })

    return {
        "project_id": project_id,
        "project_name": proj[1],
        "total_requirements": total,
        "decided": decided,
        "tasked": tasked,
        "implemented": implemented,
        "verified": verified,
        "by_requirement": by_requirement,
    }
