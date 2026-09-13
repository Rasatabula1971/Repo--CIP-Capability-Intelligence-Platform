"""
DB queries backing the MCP tools. Pure functions of a psycopg connection —
no MCP types leak in, so these are testable without the MCP transport.

Every dict returned here is safe to hand straight to json.dumps.

Phase 4 additions:
- browse_components / search_capabilities accept an optional project_id
  and hard-filter rows that fail any of that project's constraints.
- capability_constraint_fit(project_id, capability_id) returns the
  per-constraint verdict list for one component.

Vocabulary note: `capability.kind` (pre-Phase-1) is the semantic role of
what the thing does — 'library' | 'cli' | 'service' | .... The new
`capability.component_kind` (Phase 1) is the *format* — 'library' | 'repo'
| 'agent' | 'skill' | 'mcp_tool' | 'workflow_template'. Both filters
exist because they answer different questions ("what does it do" vs
"what shape does it come in"). The overlap on 'library' is unavoidable
but the names disambiguate.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from core.policy import constraints as _c
from core.policy import compatibility as _compat
from core.policy import dependency as _dep
from core.policy import adapter as _adapter


# ---------------------------------------------------------------------------
# search_capabilities
# ---------------------------------------------------------------------------

def search_capabilities(
    conn,
    query: str,
    ecosystem: str | None = None,
    capability_kind: str | None = None,
    component_kind: str | None = None,
    runtime: str | None = None,
    cost_tier: str | None = None,
    project_id: str | None = None,
    tags: list[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    Search the CIP capability registry with relevance ranking.

    Uses trigram similarity on display_name and normalized_key for fuzzy
    matching (typo-tolerant), plus full-text search on description fields
    in metadata. Results are ranked by a composite score:

        rank = (0.5 * text_relevance) + (0.35 * intrinsic_score) + (0.15 * recency)

    Falls back to ILIKE substring matching when pg_trgm is not available
    (e.g. in test databases without the extension).

    Filters (all optional, ANDed together):
      ecosystem       — 'pypi', 'npm', 'source', ...
      capability_kind — semantic role ('library', 'cli', 'service', ...)
      component_kind  — format ('library', 'repo', 'agent', 'skill',
                        'mcp_tool', 'workflow_template')
      runtime         — 'python_import', 'mcp_stdio', 'claude_skill', ...
      cost_tier       — 'free', 'free_tier', 'cheap_paid', 'paid'
      tags            — list of topic strings; rows must contain ALL tags
    """
    q = (query or "").strip()
    if not q:
        return []
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100

    has_trgm = _has_pg_trgm(conn)

    if has_trgm:
        sql = _SEARCH_SQL_TRGM
    else:
        sql = _SEARCH_SQL_ILIKE

    params: dict[str, Any] = {
        "query": q,
        "query_lower": q.lower(),
        "pat": f"%{q}%",
        "ecosystem": ecosystem,
        "capability_kind": capability_kind,
        "component_kind": component_kind,
        "runtime": runtime,
        "cost_tier": cost_tier,
        "limit": limit,
    }

    tag_clause = ""
    if tags:
        tag_clauses = []
        for i, tag in enumerate(tags):
            key = f"tag_{i}"
            tag_clauses.append(
                f"c.metadata -> 'topics' @> to_jsonb(%({key})s::text)"
            )
            params[key] = tag
        tag_clause = " AND " + " AND ".join(tag_clauses)

    sql = sql.replace("/* TAG_FILTER */", tag_clause)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    rows = _filter_by_constraints(conn, rows, project_id)
    return [_json_safe(row) for row in rows]


def _has_pg_trgm(conn) -> bool:
    """Check whether the pg_trgm extension is installed."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'"
            )
            return cur.fetchone() is not None
    except Exception:
        return False


_SEARCH_SQL_TRGM = """
    SELECT
        c.id::text          AS id,
        c.normalized_key,
        c.display_name,
        c.ecosystem,
        c.kind              AS capability_kind,
        c.component_kind,
        c.runtime,
        c.cost_tier,
        c.license_spdx,
        c.license_status,
        v.id::text          AS head_version_id,
        v.display_version,
        s.total_score,
        s.confidence,
        GREATEST(
            similarity(lower(c.display_name), %(query_lower)s),
            similarity(lower(c.normalized_key), %(query_lower)s),
            CASE WHEN lower(c.display_name) LIKE '%%' || %(query_lower)s || '%%'
                 THEN 0.3 ELSE 0 END,
            CASE WHEN lower(c.normalized_key) LIKE '%%' || %(query_lower)s || '%%'
                 THEN 0.3 ELSE 0 END,
            CASE WHEN to_tsvector('english',
                    COALESCE(c.metadata->>'description', '') || ' ' ||
                    COALESCE(c.metadata->>'gemini_description', '')
                 ) @@ plainto_tsquery('english', %(query)s)
                 THEN 0.25 ELSE 0 END
        )                   AS text_relevance
    FROM capability c
    LEFT JOIN LATERAL (
        SELECT id, display_version
        FROM capability_version
        WHERE capability_id = c.id
          AND superseded_by_id IS NULL
        ORDER BY created_at DESC
        LIMIT 1
    ) v ON TRUE
    LEFT JOIN LATERAL (
        SELECT total_score, confidence
        FROM scorecard
        WHERE capability_version_id = v.id
        ORDER BY computed_at DESC
        LIMIT 1
    ) s ON TRUE
    WHERE (
            lower(c.display_name) %% %(query_lower)s
         OR lower(c.normalized_key) %% %(query_lower)s
         OR c.display_name ILIKE %(pat)s
         OR c.normalized_key ILIKE %(pat)s
         OR to_tsvector('english',
                COALESCE(c.metadata->>'description', '') || ' ' ||
                COALESCE(c.metadata->>'gemini_description', '')
            ) @@ plainto_tsquery('english', %(query)s)
    )
      AND (%(ecosystem)s::text       IS NULL OR c.ecosystem       = %(ecosystem)s)
      AND (%(capability_kind)s::text IS NULL OR c.kind            = %(capability_kind)s)
      AND (%(component_kind)s::text  IS NULL OR c.component_kind  = %(component_kind)s)
      AND (%(runtime)s::text         IS NULL OR c.runtime         = %(runtime)s)
      AND (%(cost_tier)s::text       IS NULL OR c.cost_tier       = %(cost_tier)s)
      /* TAG_FILTER */
    ORDER BY
        (0.50 * GREATEST(
            similarity(lower(c.display_name), %(query_lower)s),
            similarity(lower(c.normalized_key), %(query_lower)s),
            CASE WHEN lower(c.display_name) LIKE '%%' || %(query_lower)s || '%%'
                 THEN 0.3 ELSE 0 END,
            CASE WHEN lower(c.normalized_key) LIKE '%%' || %(query_lower)s || '%%'
                 THEN 0.3 ELSE 0 END,
            CASE WHEN to_tsvector('english',
                    COALESCE(c.metadata->>'description', '') || ' ' ||
                    COALESCE(c.metadata->>'gemini_description', '')
                 ) @@ plainto_tsquery('english', %(query)s)
                 THEN 0.25 ELSE 0 END
         )
         + 0.35 * COALESCE(s.total_score, 0)
         + 0.15 * LEAST(1.0,
             EXTRACT(EPOCH FROM (now() - c.first_seen_at)) /
             EXTRACT(EPOCH FROM INTERVAL '365 days')
           )
        ) DESC,
        c.display_name ASC
    LIMIT %(limit)s
"""

_SEARCH_SQL_ILIKE = """
    SELECT
        c.id::text          AS id,
        c.normalized_key,
        c.display_name,
        c.ecosystem,
        c.kind              AS capability_kind,
        c.component_kind,
        c.runtime,
        c.cost_tier,
        c.license_spdx,
        c.license_status,
        v.id::text          AS head_version_id,
        v.display_version,
        s.total_score,
        s.confidence,
        CASE WHEN lower(c.display_name) = %(query_lower)s THEN 1.0
             WHEN lower(c.display_name) LIKE %(query_lower)s || '%%' THEN 0.8
             WHEN lower(c.normalized_key) LIKE %(query_lower)s || '%%' THEN 0.7
             WHEN c.display_name ILIKE %(pat)s THEN 0.5
             WHEN c.normalized_key ILIKE %(pat)s THEN 0.4
             ELSE 0.3
        END                 AS text_relevance
    FROM capability c
    LEFT JOIN LATERAL (
        SELECT id, display_version
        FROM capability_version
        WHERE capability_id = c.id
          AND superseded_by_id IS NULL
        ORDER BY created_at DESC
        LIMIT 1
    ) v ON TRUE
    LEFT JOIN LATERAL (
        SELECT total_score, confidence
        FROM scorecard
        WHERE capability_version_id = v.id
        ORDER BY computed_at DESC
        LIMIT 1
    ) s ON TRUE
    WHERE (
            c.display_name ILIKE %(pat)s
         OR c.normalized_key ILIKE %(pat)s
    )
      AND (%(ecosystem)s::text       IS NULL OR c.ecosystem       = %(ecosystem)s)
      AND (%(capability_kind)s::text IS NULL OR c.kind            = %(capability_kind)s)
      AND (%(component_kind)s::text  IS NULL OR c.component_kind  = %(component_kind)s)
      AND (%(runtime)s::text         IS NULL OR c.runtime         = %(runtime)s)
      AND (%(cost_tier)s::text       IS NULL OR c.cost_tier       = %(cost_tier)s)
      /* TAG_FILTER */
    ORDER BY
        (0.50 * CASE WHEN lower(c.display_name) = %(query_lower)s THEN 1.0
                     WHEN lower(c.display_name) LIKE %(query_lower)s || '%%' THEN 0.8
                     WHEN lower(c.normalized_key) LIKE %(query_lower)s || '%%' THEN 0.7
                     WHEN c.display_name ILIKE %(pat)s THEN 0.5
                     WHEN c.normalized_key ILIKE %(pat)s THEN 0.4
                     ELSE 0.3
                END
         + 0.35 * COALESCE(s.total_score, 0)
         + 0.15 * LEAST(1.0,
             EXTRACT(EPOCH FROM (now() - c.first_seen_at)) /
             EXTRACT(EPOCH FROM INTERVAL '365 days')
           )
        ) DESC,
        c.display_name ASC
    LIMIT %(limit)s
"""


# ---------------------------------------------------------------------------
# browse_components
# ---------------------------------------------------------------------------

def browse_components(
    conn,
    component_kind: str | None = None,
    ecosystem: str | None = None,
    runtime: str | None = None,
    cost_tier: str | None = None,
    project_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """
    Enumerate components without a keyword. This is the discovery surface
    — the caller doesn't know what to search for, they want to see what
    exists in a category.

    All filters optional. With no filters, returns the top `limit`
    components across the whole registry, ranked by intrinsic score
    (unscored last).
    """
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500

    sql = """
        SELECT
            c.id::text          AS id,
            c.normalized_key,
            c.display_name,
            c.ecosystem,
            c.kind              AS capability_kind,
            c.component_kind,
            c.runtime,
            c.cost_tier,
            c.license_spdx,
            c.license_status,
            v.id::text          AS head_version_id,
            v.display_version,
            s.total_score,
            s.confidence
        FROM capability c
        LEFT JOIN LATERAL (
            SELECT id, display_version
            FROM capability_version
            WHERE capability_id = c.id
              AND superseded_by_id IS NULL
            ORDER BY created_at DESC
            LIMIT 1
        ) v ON TRUE
        LEFT JOIN LATERAL (
            SELECT total_score, confidence
            FROM scorecard
            WHERE capability_version_id = v.id
            ORDER BY computed_at DESC
            LIMIT 1
        ) s ON TRUE
        WHERE
              (%(component_kind)s::text IS NULL OR c.component_kind = %(component_kind)s)
          AND (%(ecosystem)s::text      IS NULL OR c.ecosystem      = %(ecosystem)s)
          AND (%(runtime)s::text        IS NULL OR c.runtime        = %(runtime)s)
          AND (%(cost_tier)s::text      IS NULL OR c.cost_tier      = %(cost_tier)s)
        ORDER BY
            COALESCE(s.total_score, 0) DESC,
            c.display_name ASC
        LIMIT %(limit)s
    """
    params = {
        "component_kind": component_kind,
        "ecosystem": ecosystem,
        "runtime": runtime,
        "cost_tier": cost_tier,
        "limit": limit,
    }
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    rows = _filter_by_constraints(conn, rows, project_id)
    return [_json_safe(row) for row in rows]


# ---------------------------------------------------------------------------
# capability_detail
# ---------------------------------------------------------------------------

def capability_detail(conn, capability_id: str) -> dict[str, Any] | None:
    """
    Full record for one capability: metadata (including component_kind,
    runtime, cost_tier, license_spdx), head version, interfaces (with
    input/output type descriptors when populated), declared dependencies,
    and the head version's scorecard (if any). Returns None if the
    capability is not found.
    """
    try:
        cap_uuid = uuid.UUID(capability_id)
    except (ValueError, TypeError):
        return None

    cap = _fetch_capability(conn, cap_uuid)
    if cap is None:
        return None

    head = _fetch_head_version(conn, cap_uuid)
    interfaces: list[dict[str, Any]] = []
    dependencies: list[dict[str, Any]] = []
    scorecard: dict[str, Any] | None = None
    if head is not None:
        interfaces = _fetch_interfaces(conn, head["id"])
        dependencies = _fetch_dependencies(conn, head["id"])
        scorecard = _fetch_scorecard(conn, head["id"])

    return _json_safe({
        "id": str(cap_uuid),
        "normalized_key": cap["normalized_key"],
        "display_name": cap["display_name"],
        "ecosystem": cap["ecosystem"],
        "capability_kind": cap["capability_kind"],
        "component_kind": cap["component_kind"],
        "runtime": cap["runtime"],
        "cost_tier": cap["cost_tier"],
        "license_spdx": cap["license_spdx"],
        "first_seen_at": cap["first_seen_at"],
        "head_version": head,
        "interfaces": interfaces,
        "dependencies": dependencies,
        "scorecard": scorecard,
    })


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_capability(conn, cap_id: uuid.UUID) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT normalized_key, display_name, ecosystem, kind, "
            "       component_kind, runtime, cost_tier, license_spdx, "
            "       license_status, first_seen_at "
            "FROM capability WHERE id = %s",
            (cap_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "normalized_key": row[0],
        "display_name": row[1],
        "ecosystem": row[2],
        "capability_kind": row[3],
        "component_kind": row[4],
        "runtime": row[5],
        "cost_tier": row[6],
        "license_spdx": row[7],
        "license_status": row[8],
        "first_seen_at": row[9],
    }


def _fetch_head_version(conn, cap_id: uuid.UUID) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, version_key, version_kind, display_version, created_at "
            "FROM capability_version "
            "WHERE capability_id = %s AND superseded_by_id IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (cap_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "version_key": row[1],
        "version_kind": row[2],
        "display_version": row[3],
        "created_at": row[4],
    }


def _fetch_interfaces(conn, version_id: uuid.UUID) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, name, signature, language, input_type, output_type, "
            "       evidence_item_id "
            "FROM capability_interface "
            "WHERE capability_version_id = %s "
            "ORDER BY kind, name",
            (version_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "kind": r[0],
            "name": r[1],
            "signature": r[2],
            "language": r[3],
            "input_type": r[4],
            "output_type": r[5],
            "evidence_item_id": str(r[6]),
        }
        for r in rows
    ]


def _fetch_dependencies(conn, version_id: uuid.UUID) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT depends_on_ecosystem, depends_on_name, version_spec, dep_kind, "
            "       evidence_item_id "
            "FROM capability_dependency "
            "WHERE capability_version_id = %s "
            "ORDER BY depends_on_ecosystem, depends_on_name",
            (version_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "ecosystem": r[0],
            "name": r[1],
            "version_spec": r[2],
            "kind": r[3],
            "evidence_item_id": str(r[4]),
        }
        for r in rows
    ]


def _fetch_scorecard(conn, version_id: uuid.UUID) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, total_score, confidence, computed_hash, computed_at "
            "FROM scorecard "
            "WHERE capability_version_id = %s "
            "ORDER BY computed_at DESC LIMIT 1",
            (version_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        scorecard_id = row[0]
        card = {
            "total_score": row[1],
            "confidence": row[2],
            "computed_hash": row[3],
            "computed_at": row[4],
        }
        cur.execute(
            "SELECT dimension_name, raw_score, weight, weighted_score, "
            "       coverage, contradiction, evidence_count "
            "FROM score_dimension_result "
            "WHERE scorecard_id = %s "
            "ORDER BY dimension_name",
            (scorecard_id,),
        )
        dims = [
            {
                "name": d[0],
                "raw_score": d[1],
                "weight": d[2],
                "weighted_score": d[3],
                "coverage": d[4],
                "contradiction": d[5],
                "evidence_count": d[6],
            }
            for d in cur.fetchall()
        ]
    card["dimensions"] = dims
    return card


# ---------------------------------------------------------------------------
# Phase 4 — project constraints
# ---------------------------------------------------------------------------

def _load_project_constraints(conn, project_id: str) -> list[_c.Constraint]:
    """Read all project_constraint rows for one project."""
    try:
        pid = uuid.UUID(project_id)
    except (ValueError, TypeError):
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, detail FROM project_constraint WHERE project_id = %s "
            "ORDER BY created_at",
            (pid,),
        )
        return [_c.Constraint(kind=k, detail=(d or {})) for (k, d) in cur.fetchall()]


def _row_to_material(row: dict[str, Any]) -> _c.ComponentMaterial:
    """A capability_link-style row (dict) -> ComponentMaterial. Uses the
    same field names browse/search return."""
    return _c.ComponentMaterial.from_row({
        "component_kind": row.get("component_kind"),
        "runtime": row.get("runtime"),
        "cost_tier": row.get("cost_tier"),
        "license_spdx": row.get("license_spdx"),
        # metadata isn't in the browse/search select, so pull it if missing
        "metadata": row.get("metadata") or {},
    })


def _filter_by_constraints(
    conn, rows: list[dict[str, Any]], project_id: Optional[str],
) -> list[dict[str, Any]]:
    """
    If project_id is set, load its constraints and drop rows that
    hard-fail. Rows that pass are annotated with 'constraint_verdicts'
    (list of {kind, passed, reason, detail}).

    We need each row's metadata for the evaluator; browse/search don't
    include it in their SELECT (kept lean). So we fetch metadata in
    one batched query when constraints are on.
    """
    if not project_id:
        return rows
    constraints = _load_project_constraints(conn, project_id)
    if not constraints:
        return rows

    # Batch-fetch metadata by id.
    ids = [r["id"] for r in rows]
    if not ids:
        return rows
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, metadata FROM capability WHERE id::text = ANY(%s)",
            (ids,),
        )
        meta_by_id = {row[0]: (row[1] or {}) for row in cur.fetchall()}

    kept: list[dict[str, Any]] = []
    for r in rows:
        r_meta = dict(r)
        r_meta["metadata"] = meta_by_id.get(r["id"], {})
        material = _row_to_material(r_meta)
        verdict = _c.evaluate(constraints, material)
        if verdict.hard_fail:
            continue
        r["constraint_verdicts"] = [
            {"kind": v.kind, "passed": v.passed, "reason": v.reason,
             "detail": v.detail} for v in verdict.verdicts
        ]
        kept.append(r)
    return kept


def capability_constraint_fit(
    conn, project_id: str, capability_id: str,
) -> Optional[dict[str, Any]]:
    """
    Full per-constraint verdict for one component against one project's
    constraints. Returns None if either id is malformed or not found.
    """
    try:
        _cap = uuid.UUID(capability_id)
    except (ValueError, TypeError):
        return None
    constraints = _load_project_constraints(conn, project_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, normalized_key, display_name, component_kind, "
            "       runtime, cost_tier, license_spdx, metadata "
            "FROM capability WHERE id = %s",
            (_cap,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    cols = ["id", "normalized_key", "display_name", "component_kind",
            "runtime", "cost_tier", "license_spdx", "metadata"]
    row_dict = dict(zip(cols, row))
    material = _c.ComponentMaterial.from_row(row_dict)
    result = _c.evaluate(constraints, material)
    return _json_safe({
        "capability_id": capability_id,
        "normalized_key": row_dict["normalized_key"],
        "display_name": row_dict["display_name"],
        "hard_fail": result.hard_fail,
        "verdicts": [
            {"kind": v.kind, "passed": v.passed, "reason": v.reason,
             "detail": v.detail} for v in result.verdicts
        ],
    })


# ---------------------------------------------------------------------------
# Phase 5 — compatibility between two components
# ---------------------------------------------------------------------------

def _fetch_row_for_compat(conn, cap_id: uuid.UUID) -> dict[str, Any] | None:
    """Row + one head-version interface (if any) for compat checking."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, normalized_key, display_name, component_kind, runtime "
            "FROM capability WHERE id = %s",
            (cap_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    result = {
        "id": row[0], "normalized_key": row[1], "display_name": row[2],
        "component_kind": row[3], "runtime": row[4],
    }
    # Grab the head-version's first interface as a representative I/O
    # signature. Most rows have no interfaces populated yet — that's OK,
    # compatibility.check_pair handles None gracefully.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ci.input_type, ci.output_type "
            "FROM capability_interface ci "
            "JOIN capability_version cv ON cv.id = ci.capability_version_id "
            "WHERE cv.capability_id = %s AND cv.superseded_by_id IS NULL "
            "ORDER BY ci.kind, ci.name LIMIT 1",
            (cap_id,),
        )
        iface = cur.fetchone()
    result["input_type"] = iface[0] if iface else None
    result["output_type"] = iface[1] if iface else None
    return result


def capability_compatibility(
    conn, source_id: str, target_id: str,
) -> dict[str, Any] | None:
    """
    Evaluate whether the source component can feed the target. Returns
    {source, target, verdict, reason, detail, io_type_check,
    adapter_hint} or None if either id is malformed / not found.
    """
    try:
        s_uid = uuid.UUID(source_id)
        t_uid = uuid.UUID(target_id)
    except (ValueError, TypeError):
        return None
    src = _fetch_row_for_compat(conn, s_uid)
    tgt = _fetch_row_for_compat(conn, t_uid)
    if src is None or tgt is None:
        return None
    v = _compat.check_pair(
        source={"runtime": src["runtime"], "component_kind": src["component_kind"]},
        target={"runtime": tgt["runtime"], "component_kind": tgt["component_kind"]},
        source_output_type=src["output_type"],
        target_input_type=tgt["input_type"],
    )
    return _json_safe({
        "source": {"id": src["id"], "normalized_key": src["normalized_key"],
                    "runtime": src["runtime"]},
        "target": {"id": tgt["id"], "normalized_key": tgt["normalized_key"],
                    "runtime": tgt["runtime"]},
        "verdict": v.verdict,
        "reason": v.reason,
        "detail": v.detail,
        "io_type_check": v.io_type_check,
        "adapter_hint": v.adapter_hint,
    })


# ---------------------------------------------------------------------------
# capability_license_check
# ---------------------------------------------------------------------------

def capability_license_check(
    conn,
    capability_id: str,
    project_id: str | None = None,
) -> dict[str, Any] | None:
    """
    Run the license classifier on a capability and return the result.
    Optionally applies a project's license_policy_profile.
    """
    try:
        cap_uuid = uuid.UUID(capability_id)
    except (ValueError, TypeError):
        return None

    from core.policy.license import check_license

    result = check_license(
        conn,
        capability_id=cap_uuid,
        project_id=uuid.UUID(project_id) if project_id else None,
    )

    return _json_safe({
        "capability_id": capability_id,
        "spdx_id": result.spdx_id,
        "status": result.status.value,
        "category": result.category.value,
        "reason": result.reason,
        "obligations": result.obligations,
    })


# ---------------------------------------------------------------------------
# dependency_fit — Phase 2
# ---------------------------------------------------------------------------

def _load_dependency_facts(
    conn, capability_version_id: uuid.UUID,
) -> list[_dep.DependencyFact]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fact_kind, fact_key, version_spec, required, metadata "
            "FROM dependency_fact "
            "WHERE capability_version_id = %s "
            "ORDER BY fact_kind, fact_key",
            (capability_version_id,),
        )
        return [
            _dep.DependencyFact(
                fact_kind=r[0],
                fact_key=r[1],
                version_spec=r[2],
                required=r[3],
                metadata=r[4] or {},
            )
            for r in cur.fetchall()
        ]


def _load_environment_profile(
    conn, project_id: uuid.UUID, profile_name: str = "default",
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT profile FROM environment_profile "
            "WHERE project_id = %s AND name = %s",
            (project_id, profile_name),
        )
        row = cur.fetchone()
    if row is None:
        return {}
    return row[0] or {}


def _load_package_deps(
    conn, capability_version_id: uuid.UUID,
) -> list[dict[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT depends_on_ecosystem, depends_on_name, version_spec "
            "FROM capability_dependency "
            "WHERE capability_version_id = %s",
            (capability_version_id,),
        )
        return [
            {"ecosystem": r[0], "name": r[1], "version_spec": r[2]}
            for r in cur.fetchall()
        ]


def dependency_fit(
    conn,
    capability_id: str,
    project_id: str | None = None,
    profile_name: str = "default",
) -> dict[str, Any] | None:
    """
    Check whether a capability's dependencies are satisfiable in a
    project's environment profile.

    Checks both dependency_fact rows (runtime, OS, services, etc.) and
    capability_dependency rows (package-level deps). Returns the fit
    result with hard failures and warnings.
    """
    try:
        cap_uuid = uuid.UUID(capability_id)
    except (ValueError, TypeError):
        return None

    cap = _fetch_capability(conn, cap_uuid)
    if cap is None:
        return None

    head = _fetch_head_version(conn, cap_uuid)
    if head is None:
        return _json_safe({
            "capability_id": capability_id,
            "normalized_key": cap["normalized_key"],
            "passed": True,
            "hard_failures": [],
            "warnings": [{"fact_kind": "info", "fact_key": "no_version",
                          "severity": "warning",
                          "reason": "no head version — nothing to check"}],
            "package_deps": [],
        })

    version_id = head["id"]

    facts = _load_dependency_facts(conn, version_id)
    profile: dict[str, Any] = {}
    if project_id:
        try:
            pid = uuid.UUID(project_id)
            profile = _load_environment_profile(conn, pid, profile_name)
        except (ValueError, TypeError):
            pass

    result = _dep.evaluate_dependency_fit(facts, profile)
    pkg_deps = _load_package_deps(conn, version_id)

    return _json_safe({
        "capability_id": capability_id,
        "normalized_key": cap["normalized_key"],
        "passed": result.passed,
        "hard_failures": [
            {"fact_kind": f.fact_kind, "fact_key": f.fact_key,
             "severity": f.severity.value, "reason": f.reason,
             "detail": f.detail}
            for f in result.hard_failures
        ],
        "warnings": [
            {"fact_kind": f.fact_kind, "fact_key": f.fact_key,
             "severity": f.severity.value, "reason": f.reason,
             "detail": f.detail}
            for f in result.warnings
        ],
        "package_deps": pkg_deps,
    })


def dependency_conflicts(
    conn,
    capability_id_a: str,
    capability_id_b: str,
) -> dict[str, Any] | None:
    """
    Check for package-level version conflicts between two capabilities.
    """
    try:
        a_uuid = uuid.UUID(capability_id_a)
        b_uuid = uuid.UUID(capability_id_b)
    except (ValueError, TypeError):
        return None

    cap_a = _fetch_capability(conn, a_uuid)
    cap_b = _fetch_capability(conn, b_uuid)
    if cap_a is None or cap_b is None:
        return None

    head_a = _fetch_head_version(conn, a_uuid)
    head_b = _fetch_head_version(conn, b_uuid)
    if head_a is None or head_b is None:
        return _json_safe({
            "capability_a": capability_id_a,
            "capability_b": capability_id_b,
            "conflicts": [],
            "note": "one or both capabilities have no head version",
        })

    deps_a = _load_package_deps(conn, head_a["id"])
    deps_b = _load_package_deps(conn, head_b["id"])

    conflicts = _dep.detect_version_conflicts(deps_a, deps_b)

    return _json_safe({
        "capability_a": capability_id_a,
        "capability_b": capability_id_b,
        "conflicts": conflicts,
    })


# ---------------------------------------------------------------------------
# search_symbols — Phase 3
# ---------------------------------------------------------------------------

def search_symbols(
    conn,
    query: str,
    symbol_kind: str | None = None,
    role: str | None = None,
    language: str | None = None,
    capability_id: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """
    Search capability_symbol by name or qualified_name. Uses ILIKE for
    substring matching (trigram indexes may not exist on capability_symbol).
    """
    q = (query or "").strip()
    if not q:
        return []
    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200

    cap_filter = ""
    params: dict[str, Any] = {
        "pat": f"%{q}%",
        "query_lower": q.lower(),
        "symbol_kind": symbol_kind,
        "role": role,
        "language": language,
        "limit": limit,
    }
    if capability_id:
        try:
            cap_uuid = uuid.UUID(capability_id)
            cap_filter = "AND cv.capability_id = %(cap_id)s"
            params["cap_id"] = cap_uuid
        except (ValueError, TypeError):
            pass

    sql = f"""
        SELECT
            s.id::text          AS symbol_id,
            s.module_path,
            s.symbol_kind,
            s.symbol_name,
            s.qualified_name,
            s.signature,
            s.return_type,
            s.docstring_summary,
            s.role,
            s.language,
            c.id::text          AS capability_id,
            c.normalized_key,
            c.display_name,
            cv.display_version
        FROM capability_symbol s
        JOIN capability_version cv ON cv.id = s.capability_version_id
        JOIN capability c ON c.id = cv.capability_id
        WHERE (
            s.symbol_name ILIKE %(pat)s
            OR s.qualified_name ILIKE %(pat)s
        )
        AND cv.superseded_by_id IS NULL
        AND (%(symbol_kind)s::text IS NULL OR s.symbol_kind = %(symbol_kind)s)
        AND (%(role)s::text       IS NULL OR s.role         = %(role)s)
        AND (%(language)s::text   IS NULL OR s.language     = %(language)s)
        {cap_filter}
        ORDER BY
            CASE WHEN lower(s.symbol_name) = %(query_lower)s THEN 0
                 WHEN lower(s.symbol_name) LIKE %(query_lower)s || '%%' THEN 1
                 WHEN lower(s.qualified_name) LIKE '%%.' || %(query_lower)s THEN 2
                 ELSE 3
            END,
            s.symbol_name ASC
        LIMIT %(limit)s
    """

    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return [_json_safe(row) for row in rows]


def ingest_symbols(
    conn,
    capability_version_id: str,
    source_revision_id: str | None = None,
) -> dict[str, Any] | None:
    """
    Materialize symbol evidence items into capability_symbol rows.

    Reads 'symbol' evidence from evidence_item (optionally scoped to a
    source_revision_id) and inserts corresponding capability_symbol rows.
    Skips duplicates by (capability_version_id, qualified_name).
    Returns a summary with counts.
    """
    try:
        cv_uuid = uuid.UUID(capability_version_id)
    except (ValueError, TypeError):
        return None

    rev_filter = ""
    params: dict[str, Any] = {"cv_id": cv_uuid}
    if source_revision_id:
        try:
            rev_uuid = uuid.UUID(source_revision_id)
            rev_filter = "AND ei.source_revision_id = %(rev_id)s"
            params["rev_id"] = rev_uuid
        except (ValueError, TypeError):
            return None

    sql = f"""
        SELECT ei.id, ei.extracted_value
        FROM evidence_item ei
        WHERE ei.evidence_type = 'symbol'
        {rev_filter}
        ORDER BY ei.created_at
    """

    with conn.cursor() as cur:
        cur.execute(sql, params)
        evidence_rows = cur.fetchall()

    inserted = 0
    skipped = 0

    for eid, ev in evidence_rows:
        if not isinstance(ev, dict):
            skipped += 1
            continue
        qname = ev.get("qualified_name", "")
        if not qname:
            skipped += 1
            continue

        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM capability_symbol "
                "WHERE capability_version_id = %s AND qualified_name = %s",
                (cv_uuid, qname),
            )
            if cur.fetchone():
                skipped += 1
                continue

            cur.execute(
                "INSERT INTO capability_symbol "
                "(capability_version_id, evidence_item_id, module_path, "
                " symbol_kind, symbol_name, qualified_name, signature, "
                " return_type, docstring_summary, role, language, metadata) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                (
                    cv_uuid,
                    eid,
                    ev.get("module_path", ""),
                    ev.get("symbol_kind", "function"),
                    ev.get("symbol_name", ""),
                    qname,
                    ev.get("signature"),
                    ev.get("return_type"),
                    ev.get("docstring_summary"),
                    ev.get("role", "utility"),
                    ev.get("language", "python"),
                    json.dumps({
                        k: v for k, v in ev.items()
                        if k not in ("module_path", "symbol_kind", "symbol_name",
                                     "qualified_name", "signature", "return_type",
                                     "docstring_summary", "role", "language")
                    }),
                ),
            )
            inserted += 1

    conn.commit()
    return {
        "capability_version_id": str(cv_uuid),
        "inserted": inserted,
        "skipped": skipped,
        "total_evidence": len(evidence_rows),
    }


# ---------------------------------------------------------------------------
# verify_capability — Phase 4a
# ---------------------------------------------------------------------------

def record_verification_run(
    conn,
    capability_version_id: str,
    result: str,
    environment: dict[str, Any] | None = None,
    duration_ms: int = 0,
    logs: str = "",
    artifacts: list[dict] | None = None,
    reproducibility_hash: str = "",
    triggered_by: str = "manual",
    assertions: list[dict] | None = None,
) -> dict[str, Any] | None:
    """
    Record a completed verification run and its assertions.
    Returns the run record with assertion summaries.
    """
    try:
        ver_uuid = uuid.UUID(capability_version_id)
    except (ValueError, TypeError):
        return None

    import json as _json

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO verification_run "
            "(capability_version_id, result, environment, duration_ms, "
            " logs, artifacts, reproducibility_hash, triggered_by, "
            " finished_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now()) RETURNING id",
            (
                ver_uuid,
                result,
                _json.dumps(environment or {}),
                duration_ms,
                logs,
                _json.dumps(artifacts or []),
                reproducibility_hash,
                triggered_by,
            ),
        )
        run_id = cur.fetchone()[0]

        assertion_rows = []
        for a in (assertions or []):
            cur.execute(
                "INSERT INTO verification_assertion "
                "(verification_run_id, assertion_kind, command, exit_code, "
                " stdout, stderr, duration_ms, passed, reason) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    run_id,
                    a.get("assertion_kind", ""),
                    a.get("command", ""),
                    a.get("exit_code"),
                    a.get("stdout", ""),
                    a.get("stderr", ""),
                    a.get("duration_ms", 0),
                    a.get("passed", False),
                    a.get("reason", ""),
                ),
            )
            assertion_rows.append({
                "id": str(cur.fetchone()[0]),
                "assertion_kind": a.get("assertion_kind"),
                "passed": a.get("passed", False),
                "reason": a.get("reason", ""),
            })

    conn.commit()

    return _json_safe({
        "run_id": str(run_id),
        "capability_version_id": capability_version_id,
        "result": result,
        "duration_ms": duration_ms,
        "reproducibility_hash": reproducibility_hash,
        "assertions": assertion_rows,
    })


def verification_status(
    conn,
    capability_id: str,
) -> dict[str, Any] | None:
    """
    Get verification status for a capability's head version.
    Returns the latest run with its assertions.
    """
    try:
        cap_uuid = uuid.UUID(capability_id)
    except (ValueError, TypeError):
        return None

    cap = _fetch_capability(conn, cap_uuid)
    if cap is None:
        return None

    head = _fetch_head_version(conn, cap_uuid)
    if head is None:
        return _json_safe({
            "capability_id": capability_id,
            "normalized_key": cap["normalized_key"],
            "verified": False,
            "runs": [],
            "note": "no head version",
        })

    version_id = head["id"]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, result, duration_ms, reproducibility_hash, "
            "       triggered_by, started_at, finished_at "
            "FROM verification_run "
            "WHERE capability_version_id = %s "
            "ORDER BY started_at DESC LIMIT 5",
            (version_id,),
        )
        cols = [d[0] for d in cur.description]
        runs = [dict(zip(cols, r)) for r in cur.fetchall()]

    for run in runs:
        run["id"] = str(run["id"])
        with conn.cursor() as cur:
            cur.execute(
                "SELECT assertion_kind, passed, reason, duration_ms "
                "FROM verification_assertion "
                "WHERE verification_run_id = %s "
                "ORDER BY created_at",
                (run["id"],),
            )
            run["assertions"] = [
                {"assertion_kind": r[0], "passed": r[1],
                 "reason": r[2], "duration_ms": r[3]}
                for r in cur.fetchall()
            ]

    latest_passed = runs[0]["result"] == "passed" if runs else False

    return _json_safe({
        "capability_id": capability_id,
        "normalized_key": cap["normalized_key"],
        "version_id": str(version_id),
        "display_version": head.get("display_version", ""),
        "verified": latest_passed,
        "runs": runs,
    })


# ---------------------------------------------------------------------------
# experience memory — Phase 5
# ---------------------------------------------------------------------------

def record_component_use(
    conn,
    project_id: str,
    capability_id: str,
    capability_version_id: str | None = None,
    recommendation_id: str | None = None,
    verification_run_id: str | None = None,
) -> dict[str, Any] | None:
    """Record that a project adopted a capability."""
    try:
        proj_uuid = uuid.UUID(project_id)
        cap_uuid = uuid.UUID(capability_id)
    except (ValueError, TypeError):
        return None

    ver_uuid = None
    if capability_version_id:
        try:
            ver_uuid = uuid.UUID(capability_version_id)
        except (ValueError, TypeError):
            pass

    rec_uuid = None
    if recommendation_id:
        try:
            rec_uuid = uuid.UUID(recommendation_id)
        except (ValueError, TypeError):
            pass

    vrun_uuid = None
    if verification_run_id:
        try:
            vrun_uuid = uuid.UUID(verification_run_id)
        except (ValueError, TypeError):
            pass

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO component_project_use "
            "(project_id, capability_id, capability_version_id, "
            " recommendation_id, verification_run_id) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (proj_uuid, cap_uuid, ver_uuid, rec_uuid, vrun_uuid),
        )
        use_id = cur.fetchone()[0]
    conn.commit()

    return _json_safe({
        "use_id": str(use_id),
        "project_id": project_id,
        "capability_id": capability_id,
        "status": "active",
    })


def retire_component_use(
    conn,
    use_id: str,
    reason: str = "",
    status: str = "retired",
) -> dict[str, Any] | None:
    """Mark a component use as retired/replaced/failed."""
    try:
        use_uuid = uuid.UUID(use_id)
    except (ValueError, TypeError):
        return None

    if status not in ("retired", "replaced", "failed"):
        status = "retired"

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE component_project_use "
            "SET status = %s, retired_at = now(), retirement_reason = %s "
            "WHERE id = %s AND status = 'active' "
            "RETURNING id, status",
            (status, reason, use_uuid),
        )
        row = cur.fetchone()
    if row is None:
        return None
    conn.commit()
    return {"use_id": use_id, "status": status, "reason": reason}


def log_failure_event(
    conn,
    use_id: str,
    failure_kind: str,
    severity: str = "error",
    summary: str = "",
    detail: str = "",
) -> dict[str, Any] | None:
    """Record a failure event for a component use."""
    try:
        use_uuid = uuid.UUID(use_id)
    except (ValueError, TypeError):
        return None

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO failure_event "
            "(component_use_id, failure_kind, severity, summary, detail) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (use_uuid, failure_kind, severity, summary, detail),
        )
        event_id = cur.fetchone()[0]
    conn.commit()

    return _json_safe({
        "event_id": str(event_id),
        "use_id": use_id,
        "failure_kind": failure_kind,
        "severity": severity,
    })


def resolve_failure_event(
    conn,
    event_id: str,
    resolution: str = "",
) -> dict[str, Any] | None:
    """Mark a failure event as resolved."""
    try:
        ev_uuid = uuid.UUID(event_id)
    except (ValueError, TypeError):
        return None

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE failure_event "
            "SET resolved_at = now(), resolution = %s "
            "WHERE id = %s AND resolved_at IS NULL "
            "RETURNING id",
            (resolution, ev_uuid),
        )
        row = cur.fetchone()
    if row is None:
        return None
    conn.commit()
    return {"event_id": event_id, "resolved": True, "resolution": resolution}


def _update_experience_score(
    conn,
    project_id: uuid.UUID,
    capability_id: uuid.UUID,
) -> None:
    """Recompute and upsert the experience score for a (project, capability) pair."""
    from core.project.experience import (
        ExperienceScoreInput,
        compute_experience_score,
    )
    import datetime

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM component_project_use "
            "WHERE project_id = %s AND capability_id = %s",
            (project_id, capability_id),
        )
        total_uses = cur.fetchone()[0]

        cur.execute(
            "SELECT COUNT(*) FROM failure_event fe "
            "JOIN component_project_use cpu ON cpu.id = fe.component_use_id "
            "WHERE cpu.project_id = %s AND cpu.capability_id = %s",
            (project_id, capability_id),
        )
        total_failures = cur.fetchone()[0]

        cur.execute(
            "SELECT MAX(fe.occurred_at) FROM failure_event fe "
            "JOIN component_project_use cpu ON cpu.id = fe.component_use_id "
            "WHERE cpu.project_id = %s AND cpu.capability_id = %s",
            (project_id, capability_id),
        )
        last_failure_at = cur.fetchone()[0]

        cur.execute(
            "SELECT MAX(adopted_at) FROM component_project_use "
            "WHERE project_id = %s AND capability_id = %s AND status = 'active'",
            (project_id, capability_id),
        )
        last_success_at = cur.fetchone()[0]

    inp = ExperienceScoreInput(
        total_uses=total_uses,
        total_failures=total_failures,
        last_failure_at=last_failure_at,
        last_success_at=last_success_at,
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    result = compute_experience_score(inp, now=now)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO experience_score "
            "(project_id, capability_id, total_uses, total_failures, "
            " success_rate, last_failure_at, last_success_at, score) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (project_id, capability_id) "
            "DO UPDATE SET "
            "  total_uses = EXCLUDED.total_uses, "
            "  total_failures = EXCLUDED.total_failures, "
            "  success_rate = EXCLUDED.success_rate, "
            "  last_failure_at = EXCLUDED.last_failure_at, "
            "  last_success_at = EXCLUDED.last_success_at, "
            "  score = EXCLUDED.score, "
            "  updated_at = now()",
            (
                project_id, capability_id,
                result.total_uses, result.total_failures,
                result.success_rate,
                last_failure_at, last_success_at,
                result.score,
            ),
        )
    conn.commit()


def experience_summary(
    conn,
    project_id: str,
    capability_id: str,
) -> dict[str, Any] | None:
    """
    Get the experience summary for a capability within a project.
    Includes active uses, failures, experience score, and recommendation.
    """
    try:
        proj_uuid = uuid.UUID(project_id)
        cap_uuid = uuid.UUID(capability_id)
    except (ValueError, TypeError):
        return None

    cap = _fetch_capability(conn, cap_uuid)
    if cap is None:
        return None

    _update_experience_score(conn, proj_uuid, cap_uuid)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, status, adopted_at, retired_at, retirement_reason "
            "FROM component_project_use "
            "WHERE project_id = %s AND capability_id = %s "
            "ORDER BY adopted_at DESC",
            (proj_uuid, cap_uuid),
        )
        cols = [d[0] for d in cur.description]
        uses = [dict(zip(cols, r)) for r in cur.fetchall()]

        use_ids = [u["id"] for u in uses]
        failures = []
        if use_ids:
            cur.execute(
                "SELECT fe.id::text, fe.failure_kind, fe.severity, "
                "       fe.summary, fe.occurred_at, fe.resolved_at, fe.resolution "
                "FROM failure_event fe "
                "WHERE fe.component_use_id::text = ANY(%s) "
                "ORDER BY fe.occurred_at DESC",
                (use_ids,),
            )
            fcols = [d[0] for d in cur.description]
            failures = [dict(zip(fcols, r)) for r in cur.fetchall()]

        cur.execute(
            "SELECT score, success_rate, total_uses, total_failures "
            "FROM experience_score "
            "WHERE project_id = %s AND capability_id = %s",
            (proj_uuid, cap_uuid),
        )
        score_row = cur.fetchone()

    from core.project.experience import (
        ExperienceScoreResult,
        summarize_usage,
    )

    exp_score = None
    if score_row:
        exp_score = ExperienceScoreResult(
            score=float(score_row[0]),
            success_rate=float(score_row[1]),
            total_uses=score_row[2],
            total_failures=score_row[3],
        )

    summary = summarize_usage(
        uses=uses,
        failures=failures,
        score=exp_score,
        capability_id=capability_id,
        project_id=project_id,
    )

    return _json_safe({
        "capability_id": capability_id,
        "normalized_key": cap["normalized_key"],
        "project_id": project_id,
        "active_uses": summary.active_uses,
        "total_failures": summary.total_failures,
        "unresolved_failures": summary.unresolved_failures,
        "experience_score": summary.experience_score,
        "recommendation": summary.recommendation,
        "uses": uses,
        "failures": failures,
    })


# ---------------------------------------------------------------------------
# failure-driven replacement — Phase 6
# ---------------------------------------------------------------------------

def find_replacement(
    conn,
    project_id: str,
    capability_id: str,
    failure_kind: str = "other",
    severity: str = "error",
    summary: str = "",
    limit: int = 10,
) -> dict[str, Any] | None:
    """
    Diagnose a failure and find replacement candidates for a component.
    Combines failure diagnosis, alternative search, and ranking.
    """
    from core.project.replacement import (
        diagnose_failure,
        build_replacement_criteria,
        rank_candidates,
        build_replacement_plan,
    )

    try:
        proj_uuid = uuid.UUID(project_id)
        cap_uuid = uuid.UUID(capability_id)
    except (ValueError, TypeError):
        return None

    cap = _fetch_capability(conn, cap_uuid)
    if cap is None:
        return None

    cap_row = {**cap, "id": capability_id}

    failure_history = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fe.failure_kind, fe.severity, fe.summary "
            "FROM failure_event fe "
            "JOIN component_project_use cpu ON cpu.id = fe.component_use_id "
            "WHERE cpu.project_id = %s AND cpu.capability_id = %s "
            "ORDER BY fe.occurred_at DESC LIMIT 20",
            (proj_uuid, cap_uuid),
        )
        for r in cur.fetchall():
            failure_history.append({
                "failure_kind": r[0], "severity": r[1], "summary": r[2],
            })

    diagnosis = diagnose_failure(
        failure_kind=failure_kind,
        severity=severity,
        summary=summary,
        failure_history=failure_history,
    )

    criteria = build_replacement_criteria(cap_row, diagnosis)

    search_query = " ".join(criteria.search_terms) if criteria.search_terms else cap.get("display_name", "")

    search_results = search_capabilities(
        conn,
        query=search_query,
        ecosystem=criteria.ecosystem,
        component_kind=criteria.component_kind,
        project_id=project_id,
        limit=limit + len(criteria.exclude_ids),
    )

    project_scores: dict[str, float] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT capability_id::text, score FROM experience_score "
            "WHERE project_id = %s",
            (proj_uuid,),
        )
        for r in cur.fetchall():
            project_scores[r[0]] = float(r[1])

    candidates = rank_candidates(
        search_results, criteria.exclude_ids, project_scores,
    )[:limit]

    plan = build_replacement_plan(cap_row, diagnosis, candidates)

    return _json_safe({
        "failed_capability_id": capability_id,
        "normalized_key": cap["normalized_key"],
        "diagnosis": {
            "failure_kind": diagnosis.failure_kind,
            "category": diagnosis.category,
            "urgency": diagnosis.urgency,
            "root_cause_hint": diagnosis.root_cause_hint,
            "recommended_strategy": diagnosis.recommended_strategy,
        },
        "recommendation": plan.recommendation,
        "candidates": [
            {
                "capability_id": c.capability_id,
                "normalized_key": c.normalized_key,
                "display_name": c.display_name,
                "score": c.score,
                "reason": c.reason,
            }
            for c in plan.candidates
        ],
    })


# ---------------------------------------------------------------------------
# Phase 7 — Requirement Traceability
# ---------------------------------------------------------------------------

def import_requirements(
    conn,
    project_id: str,
    yaml_text: str,
) -> dict[str, Any] | None:
    """
    Parse a YAML requirement spec and upsert into project_requirement +
    requirement_constraint. Returns summary of what was created/updated.
    """
    from core.project.requirements import (
        parse_requirements_yaml,
        RequirementParseError,
    )

    try:
        proj_uuid = uuid.UUID(project_id)
    except (ValueError, TypeError):
        return None

    with conn.cursor() as cur:
        cur.execute("SELECT id FROM project WHERE id = %s", (proj_uuid,))
        if cur.fetchone() is None:
            return None

    try:
        parsed = parse_requirements_yaml(yaml_text)
    except RequirementParseError as e:
        return {"error": str(e), "created": 0, "updated": 0}

    created = 0
    updated = 0

    for req in parsed:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM project_requirement "
                "WHERE project_id = %s AND slug = %s",
                (proj_uuid, req["slug"]),
            )
            existing = cur.fetchone()

            if existing:
                req_id = existing[0]
                cur.execute(
                    "UPDATE project_requirement SET description = %s "
                    "WHERE id = %s",
                    (req["description"], req_id),
                )
                cur.execute(
                    "DELETE FROM requirement_constraint "
                    "WHERE project_requirement_id = %s",
                    (req_id,),
                )
                updated += 1
            else:
                cur.execute(
                    "INSERT INTO project_requirement "
                    "(project_id, slug, description) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (proj_uuid, req["slug"], req["description"]),
                )
                req_id = cur.fetchone()[0]
                created += 1

            for c in req["constraints"]:
                cur.execute(
                    "INSERT INTO requirement_constraint "
                    "(project_requirement_id, kind, detail) "
                    "VALUES (%s, %s, %s)",
                    (req_id, c["kind"], json.dumps(c["detail"])),
                )

    conn.commit()

    return _json_safe({
        "project_id": project_id,
        "created": created,
        "updated": updated,
        "total": created + updated,
        "requirements": [r["slug"] for r in parsed],
    })


def requirement_coverage(
    conn,
    project_id: str,
) -> dict[str, Any] | None:
    """
    Coverage report: for each requirement in a project, show whether it
    has a recommendation, whether that recommendation's pick has been
    verified, and whether there's experience data.
    """
    try:
        proj_uuid = uuid.UUID(project_id)
    except (ValueError, TypeError):
        return None

    with conn.cursor() as cur:
        cur.execute("SELECT id FROM project WHERE id = %s", (proj_uuid,))
        if cur.fetchone() is None:
            return None

    with conn.cursor() as cur:
        cur.execute(
            "SELECT pr.id, pr.slug, pr.description "
            "FROM project_requirement pr "
            "WHERE pr.project_id = %s "
            "ORDER BY pr.slug",
            (proj_uuid,),
        )
        reqs = cur.fetchall()

    if not reqs:
        return _json_safe({
            "project_id": project_id,
            "total_requirements": 0,
            "covered": 0,
            "verified": 0,
            "with_experience": 0,
            "coverage_pct": 0.0,
            "requirements": [],
        })

    items: list[dict[str, Any]] = []
    covered_count = 0
    verified_count = 0
    experience_count = 0

    for req_id, slug, description in reqs:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT r.id, r.verdict, r.chosen_capability_version_id "
                "FROM recommendation r "
                "WHERE r.project_requirement_id = %s "
                "ORDER BY r.created_at DESC LIMIT 1",
                (req_id,),
            )
            rec = cur.fetchone()

        has_recommendation = rec is not None
        verdict = rec[1] if rec else None
        cv_id = rec[2] if rec else None

        has_verification = False
        verification_result = None
        if cv_id:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT vr.result FROM verification_run vr "
                    "WHERE vr.capability_version_id = %s "
                    "ORDER BY vr.started_at DESC LIMIT 1",
                    (cv_id,),
                )
                vr = cur.fetchone()
                if vr:
                    has_verification = True
                    verification_result = vr[0]

        has_experience = False
        experience_score = None
        if cv_id:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT cv.capability_id FROM capability_version cv "
                    "WHERE cv.id = %s",
                    (cv_id,),
                )
                cv_row = cur.fetchone()
                if cv_row:
                    cur.execute(
                        "SELECT es.score FROM experience_score es "
                        "WHERE es.project_id = %s AND es.capability_id = %s",
                        (proj_uuid, cv_row[0]),
                    )
                    es = cur.fetchone()
                    if es:
                        has_experience = True
                        experience_score = float(es[0])

        if has_recommendation:
            covered_count += 1
        if has_verification:
            verified_count += 1
        if has_experience:
            experience_count += 1

        constraint_count = 0
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM requirement_constraint "
                "WHERE project_requirement_id = %s",
                (req_id,),
            )
            constraint_count = cur.fetchone()[0]

        items.append({
            "slug": slug,
            "description": description,
            "constraint_count": constraint_count,
            "has_recommendation": has_recommendation,
            "verdict": verdict,
            "has_verification": has_verification,
            "verification_result": verification_result,
            "has_experience": has_experience,
            "experience_score": experience_score,
        })

    total = len(reqs)
    coverage_pct = round(covered_count / total * 100, 1) if total > 0 else 0.0

    return _json_safe({
        "project_id": project_id,
        "total_requirements": total,
        "covered": covered_count,
        "verified": verified_count,
        "with_experience": experience_count,
        "coverage_pct": coverage_pct,
        "requirements": items,
    })


# ---------------------------------------------------------------------------
# Phase 8 — Inspection Run Tracking
# ---------------------------------------------------------------------------

def record_inspection_run(
    conn,
    capability_version_id: str,
    extractor_name: str,
    extractor_version: str = "1.0.0",
    source_revision: str | None = None,
    metadata: dict | None = None,
) -> dict[str, Any] | None:
    """
    Start an inspection run. Returns the run record with status='running'.
    Call finish_inspection_run when done.
    """
    try:
        cv_uuid = uuid.UUID(capability_version_id)
    except (ValueError, TypeError):
        return None

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM capability_version WHERE id = %s", (cv_uuid,),
        )
        if cur.fetchone() is None:
            return None

        cur.execute(
            "INSERT INTO inspection_run "
            "(capability_version_id, extractor_name, extractor_version, "
            " source_revision, metadata) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id, started_at",
            (cv_uuid, extractor_name, extractor_version,
             source_revision, json.dumps(metadata or {})),
        )
        row = cur.fetchone()
        conn.commit()

    return _json_safe({
        "id": row[0],
        "capability_version_id": capability_version_id,
        "extractor_name": extractor_name,
        "extractor_version": extractor_version,
        "source_revision": source_revision,
        "status": "running",
        "started_at": row[1],
    })


def finish_inspection_run(
    conn,
    run_id: str,
    status: str = "completed",
    evidence_count: int = 0,
    error_detail: str | None = None,
) -> dict[str, Any] | None:
    """Mark an inspection run as completed or failed."""
    try:
        run_uuid = uuid.UUID(run_id)
    except (ValueError, TypeError):
        return None

    if status not in ("completed", "failed"):
        return None

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE inspection_run "
            "SET status = %s, finished_at = now(), "
            "    evidence_count = %s, error_detail = %s "
            "WHERE id = %s AND status = 'running' "
            "RETURNING id, finished_at",
            (status, evidence_count, error_detail, run_uuid),
        )
        row = cur.fetchone()
        if row is None:
            return None
        conn.commit()

    return _json_safe({
        "id": row[0],
        "status": status,
        "evidence_count": evidence_count,
        "finished_at": row[1],
    })


def inspection_history(
    conn,
    capability_id: str,
    extractor_name: str | None = None,
    limit: int = 20,
) -> dict[str, Any] | None:
    """
    Return inspection run history for a capability's head version.
    Optionally filtered by extractor_name.
    """
    try:
        cap_uuid = uuid.UUID(capability_id)
    except (ValueError, TypeError):
        return None

    cap = _fetch_capability(conn, cap_uuid)
    if cap is None:
        return None

    head = _fetch_head_version(conn, cap_uuid)
    if head is None:
        return _json_safe({
            "capability_id": capability_id,
            "normalized_key": cap["normalized_key"],
            "version_id": None,
            "runs": [],
            "extractors_covered": [],
            "extractors_missing": [
                "interfaces", "licenses", "manifests",
                "secrets", "symbols", "tests",
            ],
        })

    head_version_id = head["id"]

    with conn.cursor() as cur:
        if extractor_name:
            cur.execute(
                "SELECT id, extractor_name, extractor_version, "
                "       source_revision, started_at, finished_at, "
                "       status, evidence_count, error_detail "
                "FROM inspection_run "
                "WHERE capability_version_id = %s "
                "  AND extractor_name = %s "
                "ORDER BY started_at DESC LIMIT %s",
                (head_version_id, extractor_name, limit),
            )
        else:
            cur.execute(
                "SELECT id, extractor_name, extractor_version, "
                "       source_revision, started_at, finished_at, "
                "       status, evidence_count, error_detail "
                "FROM inspection_run "
                "WHERE capability_version_id = %s "
                "ORDER BY started_at DESC LIMIT %s",
                (head_version_id, limit),
            )
        rows = cur.fetchall()

    runs = []
    for r in rows:
        runs.append({
            "id": r[0],
            "extractor_name": r[1],
            "extractor_version": r[2],
            "source_revision": r[3],
            "started_at": r[4],
            "finished_at": r[5],
            "status": r[6],
            "evidence_count": r[7],
            "error_detail": r[8],
        })

    extractors_covered = sorted(set(r["extractor_name"] for r in runs))
    all_extractors = ["interfaces", "licenses", "manifests", "secrets", "symbols", "tests"]
    missing = [e for e in all_extractors if e not in extractors_covered]

    return _json_safe({
        "capability_id": capability_id,
        "normalized_key": cap["normalized_key"],
        "version_id": head_version_id,
        "runs": runs,
        "extractors_covered": extractors_covered,
        "extractors_missing": missing,
    })


# ---------------------------------------------------------------------------
# Phase 10 — Build-Gap Proof
# ---------------------------------------------------------------------------

def authorize_build(
    conn,
    recommendation_id: str,
    authorized_by: str,
    reason: str,
) -> dict[str, Any] | None:
    """
    Authorize code generation for a BUILD recommendation.
    Returns the authorization record or an error dict.
    """
    from core.policy.build_gate import (
        authorize_build as _authorize,
        BuildGateError,
    )

    try:
        rec_uuid = uuid.UUID(recommendation_id)
    except (ValueError, TypeError):
        return None

    if not authorized_by or not authorized_by.strip():
        return {"error": "authorized_by must be non-empty"}
    if not reason or not reason.strip():
        return {"error": "reason must be non-empty"}

    try:
        auth_id = _authorize(
            conn,
            recommendation_id=rec_uuid,
            authorized_by=authorized_by,
            reason=reason,
        )
        conn.commit()
    except BuildGateError as e:
        return {"error": str(e)}
    except ValueError as e:
        return {"error": str(e)}

    return _json_safe({
        "build_authorization_id": auth_id,
        "recommendation_id": recommendation_id,
        "authorized_by": authorized_by.strip(),
        "status": "authorized",
    })


def build_gate_check(
    conn,
    project_requirement_id: str,
) -> dict[str, Any] | None:
    """
    Check whether code generation is allowed for a requirement.
    """
    from core.policy.build_gate import can_generate_for

    try:
        req_uuid = uuid.UUID(project_requirement_id)
    except (ValueError, TypeError):
        return None

    decision = can_generate_for(conn, project_requirement_id=req_uuid)

    return _json_safe({
        "project_requirement_id": project_requirement_id,
        "allowed": decision.allowed,
        "reason": decision.reason,
        "recommendation_id": decision.recommendation_id,
        "build_authorization_id": decision.build_authorization_id,
    })


def build_coverage(
    conn,
    project_id: str,
) -> dict[str, Any] | None:
    """
    Build authorization coverage for a project: for each requirement,
    show recommendation verdict + whether build is authorized.
    """
    try:
        proj_uuid = uuid.UUID(project_id)
    except (ValueError, TypeError):
        return None

    with conn.cursor() as cur:
        cur.execute("SELECT id FROM project WHERE id = %s", (proj_uuid,))
        if cur.fetchone() is None:
            return None

    with conn.cursor() as cur:
        cur.execute(
            "SELECT pr.id, pr.slug, pr.description "
            "FROM project_requirement pr "
            "WHERE pr.project_id = %s ORDER BY pr.slug",
            (proj_uuid,),
        )
        reqs = cur.fetchall()

    if not reqs:
        return _json_safe({
            "project_id": project_id,
            "total_requirements": 0,
            "build_verdicts": 0,
            "authorized": 0,
            "authorization_pct": 0.0,
            "requirements": [],
        })

    items: list[dict[str, Any]] = []
    build_count = 0
    auth_count = 0

    for req_id, slug, description in reqs:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT r.id, r.verdict, r.rule_name "
                "FROM recommendation r "
                "WHERE r.project_requirement_id = %s "
                "ORDER BY r.created_at DESC LIMIT 1",
                (req_id,),
            )
            rec = cur.fetchone()

        has_recommendation = rec is not None
        verdict = rec[1] if rec else None
        rec_id = rec[0] if rec else None

        is_build = verdict == "BUILD"
        if is_build:
            build_count += 1

        has_authorization = False
        auth_id = None
        if rec_id:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM build_authorization "
                    "WHERE recommendation_id = %s",
                    (str(rec_id),),
                )
                auth_row = cur.fetchone()
                if auth_row:
                    has_authorization = True
                    auth_id = auth_row[0]
                    auth_count += 1

        items.append({
            "slug": slug,
            "description": description,
            "has_recommendation": has_recommendation,
            "verdict": verdict,
            "is_build": is_build,
            "has_authorization": has_authorization,
            "build_authorization_id": auth_id,
        })

    total = len(reqs)
    auth_pct = round(auth_count / build_count * 100, 1) if build_count > 0 else 0.0

    return _json_safe({
        "project_id": project_id,
        "total_requirements": total,
        "build_verdicts": build_count,
        "authorized": auth_count,
        "authorization_pct": auth_pct,
        "requirements": items,
    })


# ---------------------------------------------------------------------------
# Phase 9 — Adapter Generation
# ---------------------------------------------------------------------------

def create_adapter_spec(
    conn,
    source_id: str,
    target_id: str,
    adapter_hint: str = "",
    io_transform: dict | None = None,
) -> dict[str, Any] | None:
    """
    Create an adapter_spec between two capabilities. Auto-classifies the
    bridge_kind from runtimes, then generates skeleton code. Returns
    None if either id is bad or the pair already exists.
    """
    try:
        s_uuid = uuid.UUID(source_id)
        t_uuid = uuid.UUID(target_id)
    except (ValueError, TypeError):
        return None

    src = _fetch_row_for_compat(conn, s_uuid)
    tgt = _fetch_row_for_compat(conn, t_uuid)
    if src is None or tgt is None:
        return None

    src_runtime = src.get("runtime") or "unknown"
    tgt_runtime = tgt.get("runtime") or "unknown"

    bridge_kind = _adapter.classify_bridge(
        src_runtime, tgt_runtime, adapter_hint,
    )

    skeleton = _adapter.generate_skeleton(
        bridge_kind=bridge_kind,
        source_name=src.get("normalized_key", "source"),
        target_name=tgt.get("normalized_key", "target"),
        source_runtime=src_runtime,
        target_runtime=tgt_runtime,
        io_transform=io_transform,
    )

    with conn.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO adapter_spec "
                "(source_id, target_id, bridge_kind, source_runtime, "
                " target_runtime, adapter_hint, io_transform, "
                " skeleton_code, test_code, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'generated') "
                "RETURNING id, created_at",
                (
                    s_uuid, t_uuid, bridge_kind,
                    src_runtime, tgt_runtime, adapter_hint,
                    json.dumps(io_transform or {}),
                    skeleton.adapter_code, skeleton.test_code,
                ),
            )
            row = cur.fetchone()
        except Exception:
            conn.rollback()
            return None
        conn.commit()

    return _json_safe({
        "id": row[0],
        "source_id": source_id,
        "target_id": target_id,
        "bridge_kind": bridge_kind,
        "source_runtime": src_runtime,
        "target_runtime": tgt_runtime,
        "status": "generated",
        "description": skeleton.description,
        "skeleton_code": skeleton.adapter_code,
        "test_code": skeleton.test_code,
        "created_at": row[1],
    })


def get_adapter_spec(
    conn,
    source_id: str,
    target_id: str,
) -> dict[str, Any] | None:
    """Look up an existing adapter_spec by source+target pair."""
    try:
        s_uuid = uuid.UUID(source_id)
        t_uuid = uuid.UUID(target_id)
    except (ValueError, TypeError):
        return None

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, bridge_kind, source_runtime, target_runtime, "
            "       adapter_hint, io_transform, skeleton_code, test_code, "
            "       status, created_at, updated_at "
            "FROM adapter_spec "
            "WHERE source_id = %s AND target_id = %s",
            (s_uuid, t_uuid),
        )
        row = cur.fetchone()

    if row is None:
        return None

    return _json_safe({
        "id": row[0],
        "source_id": source_id,
        "target_id": target_id,
        "bridge_kind": row[1],
        "source_runtime": row[2],
        "target_runtime": row[3],
        "adapter_hint": row[4],
        "io_transform": row[5],
        "skeleton_code": row[6],
        "test_code": row[7],
        "status": row[8],
        "created_at": row[9],
        "updated_at": row[10],
    })


def list_adapter_specs(
    conn,
    capability_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """
    List adapter specs, optionally filtered by a capability (as source
    or target) and/or status.
    """
    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200

    conditions = []
    params: dict[str, Any] = {"limit": limit}

    if capability_id:
        try:
            cap_uuid = uuid.UUID(capability_id)
            conditions.append(
                "(a.source_id = %(cap_id)s OR a.target_id = %(cap_id)s)"
            )
            params["cap_id"] = cap_uuid
        except (ValueError, TypeError):
            return []

    if status:
        conditions.append("a.status = %(status)s")
        params["status"] = status

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    sql = f"""
        SELECT
            a.id, a.source_id, a.target_id, a.bridge_kind,
            a.source_runtime, a.target_runtime, a.status,
            a.created_at,
            cs.normalized_key AS source_key,
            ct.normalized_key AS target_key
        FROM adapter_spec a
        JOIN capability cs ON cs.id = a.source_id
        JOIN capability ct ON ct.id = a.target_id
        {where}
        ORDER BY a.created_at DESC
        LIMIT %(limit)s
    """

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return [
        _json_safe({
            "id": r[0],
            "source_id": r[1],
            "target_id": r[2],
            "bridge_kind": r[3],
            "source_runtime": r[4],
            "target_runtime": r[5],
            "status": r[6],
            "created_at": r[7],
            "source_key": r[8],
            "target_key": r[9],
        })
        for r in rows
    ]


def update_adapter_status(
    conn,
    adapter_spec_id: str,
    status: str,
) -> dict[str, Any] | None:
    """Advance an adapter_spec's status (draft→generated→reviewed→tested)."""
    try:
        spec_uuid = uuid.UUID(adapter_spec_id)
    except (ValueError, TypeError):
        return None

    valid = ("draft", "generated", "reviewed", "tested")
    if status not in valid:
        return None

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE adapter_spec SET status = %s, updated_at = now() "
            "WHERE id = %s RETURNING id, status, updated_at",
            (status, spec_uuid),
        )
        row = cur.fetchone()
    if row is None:
        return None
    conn.commit()

    return _json_safe({
        "id": row[0],
        "status": row[1],
        "updated_at": row[2],
    })


# ---------------------------------------------------------------------------
# JSON-safety pass: UUIDs, Decimals, datetimes → primitives
# ---------------------------------------------------------------------------

def _json_safe(obj: Any) -> Any:
    import datetime
    import decimal
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    return obj
