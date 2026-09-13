"""
DB queries for the evaluation store (migration 029).

Durable home for repo-scout / co-work asset evaluations. Pure functions
of a psycopg connection — no MCP types leak in. Every dict returned is
safe to hand to json.dumps.

The evaluation store is the "proper" replacement for markdown-in-memory
evals: one row per asset (upsert by slug), recalled by problem_types so
a future build finds banked references even when no project named them
at evaluation time.
"""
from __future__ import annotations

import json
from typing import Any, Optional

_VALID_VERDICTS = {"Adopt", "Adapt", "Reference", "Reject", "Build"}
_VALID_ASSET_TYPES = {"repo", "skill", "agent", "mcp_server", "prompt", "workflow"}
_VALID_QUALITY = {"solid", "acceptable", "fragile", "untested", None}

_COLUMNS = """
    id::text, asset_slug, asset_type, url, verdict, code_quality, banked,
    summary, code_quality_notes, useful_parts, skip_notes, banked_references,
    projects, problem_types, license_notes, cip_status,
    evaluated_at::text, created_at::text, updated_at::text
"""


def _row_to_dict(cur) -> list[dict[str, Any]]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def record_evaluation(
    conn,
    *,
    asset_slug: str,
    verdict: str,
    asset_type: str = "repo",
    url: Optional[str] = None,
    code_quality: Optional[str] = None,
    banked: bool = False,
    summary: str = "",
    code_quality_notes: str = "",
    useful_parts: str = "",
    skip_notes: str = "",
    banked_references: Optional[list[dict]] = None,
    projects: Optional[list[str]] = None,
    problem_types: Optional[list[str]] = None,
    license_notes: str = "",
    cip_status: str = "not-ingested",
    evaluated_at: Optional[str] = None,
) -> dict[str, Any]:
    """
    Insert or update one evaluation. Upserts by asset_slug — re-evaluating
    the same asset overwrites the prior row rather than duplicating it.

    Returns the stored evaluation, or {"error": ...} on invalid input.
    """
    slug = (asset_slug or "").strip()
    if not slug:
        return {"error": "asset_slug is required"}
    if verdict not in _VALID_VERDICTS:
        return {"error": f"invalid verdict '{verdict}' (must be one of {sorted(_VALID_VERDICTS)})"}
    if asset_type not in _VALID_ASSET_TYPES:
        return {"error": f"invalid asset_type '{asset_type}' (must be one of {sorted(_VALID_ASSET_TYPES)})"}
    if code_quality not in _VALID_QUALITY:
        return {"error": f"invalid code_quality '{code_quality}'"}

    params = {
        "asset_slug": slug,
        "asset_type": asset_type,
        "url": url,
        "verdict": verdict,
        "code_quality": code_quality,
        "banked": banked,
        "summary": summary,
        "code_quality_notes": code_quality_notes,
        "useful_parts": useful_parts,
        "skip_notes": skip_notes,
        "banked_references": json.dumps(banked_references or []),
        "projects": projects or [],
        "problem_types": problem_types or [],
        "license_notes": license_notes,
        "cip_status": cip_status,
        "evaluated_at": evaluated_at,
    }

    sql = f"""
        INSERT INTO evaluation (
            asset_slug, asset_type, url, verdict, code_quality, banked,
            summary, code_quality_notes, useful_parts, skip_notes,
            banked_references, projects, problem_types, license_notes,
            cip_status, evaluated_at
        ) VALUES (
            %(asset_slug)s, %(asset_type)s, %(url)s, %(verdict)s,
            %(code_quality)s, %(banked)s, %(summary)s, %(code_quality_notes)s,
            %(useful_parts)s, %(skip_notes)s, %(banked_references)s,
            %(projects)s, %(problem_types)s, %(license_notes)s,
            %(cip_status)s, %(evaluated_at)s
        )
        ON CONFLICT (asset_slug) DO UPDATE SET
            asset_type         = EXCLUDED.asset_type,
            url                = EXCLUDED.url,
            verdict            = EXCLUDED.verdict,
            code_quality       = EXCLUDED.code_quality,
            banked             = EXCLUDED.banked,
            summary            = EXCLUDED.summary,
            code_quality_notes = EXCLUDED.code_quality_notes,
            useful_parts       = EXCLUDED.useful_parts,
            skip_notes         = EXCLUDED.skip_notes,
            banked_references  = EXCLUDED.banked_references,
            projects           = EXCLUDED.projects,
            problem_types      = EXCLUDED.problem_types,
            license_notes      = EXCLUDED.license_notes,
            cip_status         = EXCLUDED.cip_status,
            evaluated_at       = EXCLUDED.evaluated_at,
            updated_at         = now()
        RETURNING {_COLUMNS}
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        result = _row_to_dict(cur)[0]
    conn.commit()
    return result


def get_evaluation(conn, asset_slug: str) -> Optional[dict[str, Any]]:
    """Fetch one evaluation by slug, or None if not found."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM evaluation WHERE asset_slug = %(slug)s",
            {"slug": (asset_slug or "").strip()},
        )
        rows = _row_to_dict(cur)
    return rows[0] if rows else None


def search_evaluations(
    conn,
    query: Optional[str] = None,
    problem_types: Optional[list[str]] = None,
    verdict: Optional[str] = None,
    asset_type: Optional[str] = None,
    banked: Optional[bool] = None,
    project: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """
    Search the evaluation store. All filters optional and ANDed.

    problem_types: rows whose problem_types array overlaps ANY of these
      (the primary recall path during a build).
    query: substring match across asset_slug, summary, useful_parts.
    project: rows whose projects array contains this project.
    """
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500

    params: dict[str, Any] = {"limit": limit}
    where = ["TRUE"]

    if query:
        params["pat"] = f"%{query.strip()}%"
        where.append(
            "(asset_slug ILIKE %(pat)s OR summary ILIKE %(pat)s "
            "OR useful_parts ILIKE %(pat)s)"
        )
    if problem_types:
        params["problem_types"] = problem_types
        where.append("problem_types && %(problem_types)s")
    if verdict:
        params["verdict"] = verdict
        where.append("verdict = %(verdict)s")
    if asset_type:
        params["asset_type"] = asset_type
        where.append("asset_type = %(asset_type)s")
    if banked is not None:
        params["banked"] = banked
        where.append("banked = %(banked)s")
    if project:
        params["project"] = [project]
        where.append("projects && %(project)s")

    sql = f"""
        SELECT {_COLUMNS} FROM evaluation
        WHERE {' AND '.join(where)}
        ORDER BY updated_at DESC
        LIMIT %(limit)s
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return _row_to_dict(cur)


def list_evaluations(
    conn,
    banked: Optional[bool] = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """List evaluations, newest first. Optionally filter to banked only."""
    return search_evaluations(conn, banked=banked, limit=limit)
