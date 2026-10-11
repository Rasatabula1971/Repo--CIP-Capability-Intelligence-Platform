"""
Fact loading for the PDR-to-Build workflow.

Every function here reads rows and returns plain dicts, or writes one
audit row. No decisions are made in this module: the rules live in
core.pdr, and the tool-level orchestration in mcp_server.pdr_queries.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import uuid
from typing import Any

from core.pdr import lock as lock_rules
from core.pdr import requirements as req_rules
from core.pdr.spans import MAX_QUOTE_CHARS, Span, span_coverage, split_sections
from mcp_server import pdr_approvals

# Order that decides which source revision a capability version is pinned
# to. Evidence is read for that revision and no other, here and in
# workers.recommend: a license read on an older revision says nothing
# about the one that would be installed.
PINNED_REVISION_ORDER = "sr.identified_at DESC, sr.id DESC"

# One definition of "a license CIP identified", shared with the fit matcher's
# loader so the two cannot drift: see workers.recommend.IDENTIFIED_LICENSE.
from workers.recommend import IDENTIFIED_LICENSE  # noqa: E402


def canon_uuid(s: Any) -> str | None:
    """
    The canonical form of a UUID, or None if `s` is not one written the
    usual way. uuid.UUID() also accepts braces, a urn: prefix, upper case
    and no hyphens. Those spellings then fail to match the canonical
    strings held in memory, or reach PostgreSQL in a form it rejects, and
    a tool raises instead of answering.
    """
    if isinstance(s, uuid.UUID):
        return str(s)
    if not isinstance(s, str):
        return None
    text = s.strip().lower()
    try:
        canonical = str(uuid.UUID(text))
    except (ValueError, AttributeError, TypeError):
        return None
    return canonical if canonical == text else None


def is_uuid(s: Any) -> bool:
    return canon_uuid(s) is not None


_SECTIONS_CACHE: dict[str, tuple] = {}


def sections_for(source: dict[str, Any]) -> tuple:
    """Sections of a stored PDR, split once per text and kept."""
    key = source["content_hash"]
    if key not in _SECTIONS_CACHE:
        if len(_SECTIONS_CACHE) >= 16:
            _SECTIONS_CACHE.pop(next(iter(_SECTIONS_CACHE)))
        _SECTIONS_CACHE[key] = split_sections(source["raw_text"])
    return _SECTIONS_CACHE[key]


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def audit(
    conn,
    *,
    project_id: str | None,
    event_type: str,
    subject_table: str | None = None,
    subject_id: str | None = None,
    payload: dict[str, Any] | None = None,
    actor: str = "mcp",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_event
                (project_id, event_type, actor, subject_table, subject_id, payload)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            """,
            (project_id, event_type, actor, subject_table, subject_id,
             json.dumps(payload or {}, default=str)),
        )


# ---------------------------------------------------------------------------
# Project + source
# ---------------------------------------------------------------------------

def get_project(conn, project_id: str) -> dict[str, Any] | None:
    project_id = canon_uuid(project_id)
    if not project_id:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT id, name, metadata FROM project WHERE id = %s", (project_id,))
        row = cur.fetchone()
    if not row:
        return None
    return {"id": str(row[0]), "name": row[1], "metadata": row[2] or {}}


def find_project_by_name(conn, name: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT id, name, metadata FROM project WHERE name = %s", (name,))
        row = cur.fetchone()
    if not row:
        return None
    return {"id": str(row[0]), "name": row[1], "metadata": row[2] or {}}


def latest_source(conn, project_id: str) -> dict[str, Any] | None:
    """The current PDR for a project: the most recently activated source."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT rs.id, rs.title, rs.content_hash, rs.raw_text, rs.ingested_at,
                   rs.supersedes_id, rs.raw_ref, rs.source_file_hash, rs.activated_at,
                   (SELECT count(*) FROM requirement_source o
                     WHERE o.project_id = rs.project_id) AS source_count
            FROM requirement_source rs
            WHERE rs.project_id = %s
            ORDER BY rs.activated_at DESC, rs.ingested_at DESC, rs.id DESC
            LIMIT 1
            """,
            (project_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "id": str(row[0]), "title": row[1], "content_hash": row[2],
        "raw_text": row[3], "ingested_at": row[4].isoformat(),
        "supersedes_id": str(row[5]) if row[5] else None,
        "raw_ref": row[6], "source_file_hash": row[7],
        "activated_at": row[8].isoformat(),
        # More than one text has been the PDR for this project. True for a
        # replacement and equally for a return to an earlier text, which
        # supersedes_id alone would not show.
        "replaced_another": row[9] > 1,
    }


# ---------------------------------------------------------------------------
# Scope: requirements, needs, links
# ---------------------------------------------------------------------------

def load_scope(conn, project_id: str) -> dict[str, Any]:
    """
    Active PDR requirements and capability needs for a project, shaped
    for core.pdr.requirements (snapshot, blockers) and for display.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, slug, kind, priority, description, disposition,
                   disposition_reason, requirement_source_id, source_span,
                   search_terms, problem_types
            FROM project_requirement
            WHERE project_id = %s AND status = 'active'
            ORDER BY slug
            """,
            (project_id,),
        )
        rows = cur.fetchall()

        cur.execute(
            """
            SELECT ac.id, ac.project_requirement_id, ac.ordinal, ac.text
            FROM acceptance_criterion ac
            JOIN project_requirement pr ON pr.id = ac.project_requirement_id
            WHERE pr.project_id = %s
            ORDER BY ac.project_requirement_id, ac.ordinal
            """,
            (project_id,),
        )
        criteria_by_req: dict[str, list[dict[str, Any]]] = {}
        for cid, rid, ordinal, text in cur.fetchall():
            criteria_by_req.setdefault(str(rid), []).append(
                {"id": str(cid), "ordinal": ordinal, "text": text}
            )

        cur.execute(
            """
            SELECT rc.project_requirement_id, rc.kind, rc.detail
            FROM requirement_constraint rc
            JOIN project_requirement pr ON pr.id = rc.project_requirement_id
            WHERE pr.project_id = %s
            ORDER BY rc.project_requirement_id, rc.id
            """,
            (project_id,),
        )
        constraints_by_req: dict[str, list[dict[str, Any]]] = {}
        for rid, kind, detail in cur.fetchall():
            constraints_by_req.setdefault(str(rid), []).append(
                {"kind": kind, "detail": detail or {}}
            )

        cur.execute(
            """
            SELECT l.requirement_id, l.need_id
            FROM requirement_need_link l
            JOIN project_requirement r ON r.id = l.requirement_id
            JOIN project_requirement n ON n.id = l.need_id
            WHERE r.project_id = %s AND r.status = 'active' AND n.status = 'active'
            """,
            (project_id,),
        )
        links = [(str(a), str(b)) for a, b in cur.fetchall()]

        # The text each anchored requirement points into, to check the anchor.
        source_ids = sorted({str(r[7]) for r in rows if r[7] and r[2] != req_rules.NEED_KIND})
        texts: dict[str, tuple[str | None, str]] = {}
        if source_ids:
            cur.execute(
                "SELECT id, raw_text, content_hash FROM requirement_source "
                "WHERE id = ANY(%s::uuid[])",
                (source_ids,),
            )
            texts = {str(r[0]): (r[1], r[2]) for r in cur.fetchall()}

    slug_by_id = {str(r[0]): r[1] for r in rows}
    needs_of_req: dict[str, list[str]] = {}
    reqs_of_need: dict[str, list[str]] = {}
    for req_id, need_id in links:
        needs_of_req.setdefault(req_id, []).append(slug_by_id[need_id])
        reqs_of_need.setdefault(need_id, []).append(slug_by_id[req_id])

    requirements: list[dict[str, Any]] = []
    needs: list[dict[str, Any]] = []
    for (rid, slug, kind, priority, description, disposition, reason,
         source_id, span, search_terms, problem_types) in rows:
        rid = str(rid)
        if kind == req_rules.NEED_KIND:
            constraints = constraints_by_req.get(rid, [])
            needs.append({
                "id": rid,
                "slug": slug,
                "description": description,
                "search_terms": list(search_terms or []),
                "problem_types": list(problem_types or []),
                "constraints": constraints,
                "satisfies": sorted(reqs_of_need.get(rid, [])),
                "fingerprint": req_rules.need_fingerprint(
                    search_terms=list(search_terms or []), constraints=constraints,
                    problem_types=list(problem_types or []),
                ),
            })
        else:
            # v1.0 stored whatever the caller sent: a string, a list, a
            # quote that is a number. Anything that is not the v1.1 shape
            # reads as "no quote", which the confirmation blockers report.
            span = span if isinstance(span, dict) else {}
            quote = span.get("quote") if isinstance(span.get("quote"), str) else None
            criteria = criteria_by_req.get(rid, [])
            has_span = (isinstance(span.get("start"), int) and isinstance(span.get("end"), int)
                        and not isinstance(span.get("start"), bool)
                        and 0 <= span["start"] < span["end"])
            # A stored anchor is only an anchor if it still says what the
            # stored text says there. Rows migrated from v1.0 carry offsets
            # and quotes the caller made up; rows carried across a re-ingest
            # were located in the new text but under the old rules.
            span_issue = None
            text, text_hash_ = texts.get(str(source_id), (None, "")) if source_id else (None, "")
            if has_span and text is not None:
                if quote is None or text[span["start"]:span["end"]] != quote:
                    has_span, span_issue = False, "not_in_text"
                elif len(quote) > MAX_QUOTE_CHARS:
                    span_issue = "too_long"
                elif _crosses_heading(text, text_hash_, span["start"], span["end"]):
                    span_issue = "crosses_heading"
            requirements.append({
                "id": rid,
                "slug": slug,
                "kind": kind,
                "priority": priority,
                "text": description,
                "disposition": disposition,
                "disposition_reason": reason,
                "source_id": str(source_id) if source_id else None,
                "span": {"start": span.get("start"), "end": span.get("end")} if has_span else None,
                "has_span": has_span,
                "span_issue": span_issue,
                "quote": quote,
                "criteria": criteria,
                "acceptance_criteria": [c["text"] for c in criteria],
                "need_slugs": sorted(needs_of_req.get(rid, [])),
            })
    return {"requirements": requirements, "needs": needs}


_SECTION_STARTS: dict[str, list[int]] = {}


def _crosses_heading(text: str, content_hash: str, start: int, end: int) -> bool:
    """True if a section starts strictly inside [start, end)."""
    if content_hash not in _SECTION_STARTS:
        if len(_SECTION_STARTS) >= 16:
            _SECTION_STARTS.pop(next(iter(_SECTION_STARTS)))
        sections = sections_for({"content_hash": content_hash, "raw_text": text})
        _SECTION_STARTS[content_hash] = sorted(sec.start for sec in sections)
    starts = _SECTION_STARTS[content_hash]
    i = bisect.bisect_right(starts, start)
    return i < len(starts) and starts[i] < end


def scope_hash(scope: dict[str, Any], source: dict[str, Any] | None) -> tuple[dict, str]:
    snapshot = req_rules.requirements_snapshot(
        source_content_hash=source["content_hash"] if source else None,
        requirements=scope["requirements"],
        needs=scope["needs"],
    )
    return snapshot, req_rules.requirements_hash(snapshot)


def scope_blockers(scope: dict[str, Any], source: dict[str, Any] | None) -> list[str]:
    return req_rules.confirmation_blockers(
        latest_source_id=source["id"] if source else None,
        requirements=scope["requirements"],
        needs=scope["needs"],
    )


def scope_span_coverage(scope: dict[str, Any], source: dict[str, Any] | None) -> dict | None:
    if not source or source.get("raw_text") is None:
        return None
    # An anchor with a problem (too long, or across a heading) blocks
    # confirmation. Until it is fixed it does not count as mapping the
    # sections it touches either.
    anchored = [r for r in scope["requirements"]
                if r["span"] and r["source_id"] == source["id"] and not r.get("span_issue")]
    out_of_scope = ("deferred", "not_applicable")
    return span_coverage(
        source["raw_text"],
        [Span(r["span"]["start"], r["span"]["end"]) for r in anchored
         if r["disposition"] not in out_of_scope],
        [Span(r["span"]["start"], r["span"]["end"]) for r in anchored
         if r["disposition"] in out_of_scope],
        sections=sections_for(source),
    )


def load_retired(conn, project_id: str) -> list[dict[str, Any]]:
    """Requirements and needs taken out of scope, with the reason given."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT slug, kind, description, retired_reason
            FROM project_requirement
            WHERE project_id = %s AND status = 'retired'
            ORDER BY slug
            """,
            (project_id,),
        )
        return [
            {"slug": r[0], "kind": r[1], "text": r[2], "reason": r[3]}
            for r in cur.fetchall()
        ]


def replaced_banked_evaluations(conn, project_id: str, problem_types: list[str]) -> list[str]:
    """
    Evaluations that would have counted as a banked match for these
    problem types and were rewritten into something that no longer does.

    The evaluation store upserts by slug. Without this, the way to avoid a
    "BUILD despite a banked match" flag would be to overwrite the banked
    evaluation first.

    "Counted as a banked match" is the search's own test: a tag in common,
    and a verdict other than Reject or the banked flag set. The window
    opens 30 days before the project was created, so an overwrite made in
    preparation is seen as well as one made during the build.
    """
    wanted = sorted(set(problem_types or []))
    if not wanted:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ae.subject_id
            FROM audit_event ae
            JOIN project p ON p.id = %s
            WHERE ae.event_type = 'evaluation_replaced'
              AND ae.created_at >= p.created_at - interval '30 days'
              AND (COALESCE((ae.payload->'before'->>'banked')::boolean, false)
                   OR COALESCE(ae.payload->'before'->>'verdict', '') <> 'Reject')
              AND ae.payload->'before'->'problem_types' ?| %s::text[]
              AND NOT (
                  (COALESCE((ae.payload->'after'->>'banked')::boolean, false)
                   OR COALESCE(ae.payload->'after'->>'verdict', '') <> 'Reject')
                  AND ae.payload->'after'->'problem_types' ?| %s::text[]
              )
            ORDER BY 1
            """,
            (project_id, wanted, wanted),
        )
        return [r[0] for r in cur.fetchall()]


def record_files_written_by_tool(conn, tool: str, root: str, relative_paths: list[str]) -> int:
    """
    Log every file a CIP tool wrote, with its SHA-256. A tool that writes
    files is a way to turn text an agent supplied into "a file on disk":
    ingested as the PDR it would be shown with a path and a matching hash,
    like a document a person provided. The log lets the summary say so.
    Does not commit. Returns the number of files logged.
    """
    from pathlib import Path
    logged = 0
    for rel in relative_paths:
        path = Path(root) / rel
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        audit(conn, project_id=None, event_type="file_written_by_tool",
              subject_table=None, subject_id=digest,
              payload={"tool": tool, "path": str(path), "sha256": digest})
        logged += 1
    return logged


def file_written_by_tool(conn, file_hash: str | None) -> str | None:
    """The CIP tool that wrote a file with these exact bytes, or None."""
    if not file_hash:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload->>'tool' FROM audit_event "
            "WHERE event_type = 'file_written_by_tool' AND subject_id = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (file_hash,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def registry_size(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capability")
        return cur.fetchone()[0]


# What a sweep's answer depends on, besides the need: which capabilities
# exist, which versions and source bindings they have, and what has been
# read from them. A count of capabilities misses the event the workflow
# asks for most often: an existing match being ingested or analysed.
_STAMP_TABLES = (
    "capability", "capability_version", "capability_source_binding",
    "source_revision", "evidence_item", "scorecard", "capability_interface",
    "capability_dependency", "analysis_stage_result",
)


def registry_stamp(conn) -> str:
    """
    A fingerprint of everything a sweep reads, which changes when any of
    it does: row counts per table (they are insert-mostly), the content of
    `capability` (updated in place by re-ingest), which versions are
    superseded or retired (updated in place), how many analysis stages
    have succeeded (a stage row is updated when it ends), and the
    evaluation store.

    The evaluation store is the half of the sweep that says "something
    like this was assessed and banked before". It was left out: an
    evaluation banked after the sweep did not make the sweep stale, and
    the BUILD that followed was recorded as a cold start with nothing to
    compare against.
    """
    parts = []
    with conn.cursor() as cur:
        for table in _STAMP_TABLES:
            cur.execute(f"SELECT count(*) FROM {table}")
            parts.append(f"{table}={cur.fetchone()[0]}")
        cur.execute(
            "SELECT md5(COALESCE(string_agg(md5(c::text), '' ORDER BY c.id), '')) "
            "FROM capability c"
        )
        parts.append(f"capability_rows={cur.fetchone()[0]}")
        cur.execute("SELECT count(*) FROM analysis_stage_result WHERE status = 'succeeded'")
        parts.append(f"stages_succeeded={cur.fetchone()[0]}")
        # Order-independent checksums: one pass, no sort.
        cur.execute(
            "SELECT COALESCE(sum(hashtextextended("
            "  cv.id::text || ':' || COALESCE(cv.superseded_by_id::text, '') || ':' ||"
            "  COALESCE(cv.lifecycle_state::text, ''), 0)::numeric), 0) "
            "FROM capability_version cv"
        )
        parts.append(f"version_states={cur.fetchone()[0]}")
        cur.execute(
            "SELECT count(*), COALESCE(sum(hashtextextended(e::text, 0)::numeric), 0) "
            "FROM evaluation e"
        )
        n, checksum = cur.fetchone()
        parts.append(f"evaluations={n}:{checksum}")
    return hashlib.sha256(";".join(parts).encode("utf-8")).hexdigest()[:32]


def confirmation(conn, project_id: str, req_hash: str) -> dict[str, Any]:
    return pdr_approvals.confirmation_state(
        conn, project_id=project_id, requirements_hash=req_hash,
    )


# ---------------------------------------------------------------------------
# Searches and decisions
# ---------------------------------------------------------------------------

def load_searches(conn, need_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, query_source, queries, filters, need_fingerprint,
                   registry_size, found_count, evaluated_capability_version_ids,
                   unevaluable_count, evaluation_slugs, searched_at, truncated,
                   policy_excluded_count, registry_stamp
            FROM requirement_search
            WHERE project_requirement_id = %s
            ORDER BY searched_at, id
            """,
            (need_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "id": str(r[0]), "query_source": r[1], "queries": list(r[2]),
            "filters": r[3] or {}, "need_fingerprint": r[4],
            "registry_size": r[5], "found_count": r[6],
            "evaluated_capability_version_ids": [str(x) for x in (r[7] or [])],
            "unevaluable_count": r[8],
            "evaluation_slugs": list(r[9] or []),
            "searched_at": r[10].isoformat(),
            "truncated": bool(r[11]),
            "policy_excluded_count": r[12],
            "registry_stamp": r[13],
        }
        for r in rows
    ]


def counts_as_sweep(search: dict[str, Any], fingerprint: str) -> bool:
    """
    The mandatory search: the need's own terms, unfiltered, against the
    need as it is currently defined. A custom query or a filtered search
    is extra evidence, never a substitute.
    """
    return (
        search["query_source"] == "need_terms"
        and not search["filters"]
        and search["need_fingerprint"] == fingerprint
    )


_DECISION_COLUMNS = """
    dr.id, dr.project_requirement_id, dr.recommendation_id, dr.search_ids,
    dr.need_fingerprint, dr.computed_verdict, dr.computed_rule_name,
    dr.computed_capability_version_id, dr.final_verdict,
    dr.final_capability_version_id, dr.pinned_revision_key,
    dr.pinned_source_asset_key, dr.decided_by, dr.coverage_outcome, dr.flags,
    dr.evaluation_slugs, dr.rationale, dr.created_at
"""


def _decision_row(r) -> dict[str, Any]:
    return {
        "id": str(r[0]),
        "need_id": str(r[1]),
        "recommendation_id": str(r[2]) if r[2] else None,
        "search_ids": [str(x) for x in (r[3] or [])],
        "need_fingerprint": r[4],
        "computed_verdict": r[5],
        "computed_rule_name": r[6],
        "computed_capability_version_id": str(r[7]) if r[7] else None,
        "final_verdict": r[8],
        "final_capability_version_id": str(r[9]) if r[9] else None,
        "pinned_revision_key": r[10],
        "pinned_source_asset_key": r[11],
        "decided_by": r[12],
        "coverage_outcome": r[13],
        "flags": list(r[14] or []),
        "evaluation_slugs": list(r[15] or []),
        "rationale": r[16],
        "created_at": r[17].isoformat(),
    }


def current_decisions(conn, project_id: str) -> dict[str, dict[str, Any]]:
    """Latest decision_record per need, keyed by need id."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ON (dr.project_requirement_id) {_DECISION_COLUMNS}
            FROM decision_record dr
            JOIN project_requirement pr ON pr.id = dr.project_requirement_id
            WHERE pr.project_id = %s
            ORDER BY dr.project_requirement_id, dr.seq DESC
            """,
            (project_id,),
        )
        rows = cur.fetchall()
    return {str(r[1]): _decision_row(r) for r in rows}


def capability_facts(
    conn, cv_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """
    normalized_key, license, cost tier and pin for capability versions, as
    a decision made NOW would see them: the license is the one read from
    the revision that decision would pin.
    """
    ids = [c for c in dict.fromkeys(cv_ids) if c]
    return _facts_for(conn, [(c, c, None, None) for c in ids])


def decision_facts(
    conn, decisions: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """
    The same facts for decisions already made, keyed by DECISION id, each
    read from that decision's own pin (revision key and source asset).

    Keyed by decision, not by capability version: two needs can settle on
    one capability at different times and pin different revisions of it.
    Keyed by version, the second pin's license was printed beside the
    first pin's revision, and the lock said "pinned: R1 ... license: MIT"
    about a revision whose license CIP had read as GPL-3.0.
    """
    return _facts_for(conn, [
        (d["id"], d["final_capability_version_id"],
         d["pinned_revision_key"], d["pinned_source_asset_key"])
        for d in decisions if d["final_capability_version_id"]
    ])


def _facts_for(
    conn, wants: list[tuple[str, str, str | None, str | None]],
) -> dict[str, dict[str, Any]]:
    """wants: (result key, capability_version_id, revision key, asset key)."""
    if not wants:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH want AS (
                SELECT * FROM unnest(%s::text[], %s::uuid[], %s::text[], %s::text[])
                         AS w(tag, cv_id, pin_key, pin_asset)
            ),
            pinned AS (
                SELECT w.tag, w.cv_id,
                       (SELECT csb.source_revision_id
                          FROM capability_source_binding csb
                          JOIN source_revision sr ON sr.id = csb.source_revision_id
                          JOIN source_asset sa ON sa.id = sr.source_asset_id
                         WHERE csb.capability_version_id = w.cv_id
                           AND (w.pin_key IS NULL OR sr.revision_key = w.pin_key)
                           AND (w.pin_asset IS NULL OR sa.external_key = w.pin_asset)
                         ORDER BY {PINNED_REVISION_ORDER} LIMIT 1) AS rev_id
                FROM want w
            )
            SELECT p.tag, c.normalized_key, c.display_name, c.license_spdx, c.cost_tier,
                   (SELECT array_agg(DISTINCT ei.extracted_value->>'spdx_id')
                      FROM evidence_item ei
                     WHERE {IDENTIFIED_LICENSE}
                       AND ei.source_revision_id = p.rev_id),
                   (SELECT sr.revision_key FROM source_revision sr WHERE sr.id = p.rev_id)
            FROM pinned p
            JOIN capability_version cv ON cv.id = p.cv_id
            JOIN capability c ON c.id = cv.capability_id
            """,
            ([w[0] for w in wants], [w[1] for w in wants],
             [w[2] for w in wants], [w[3] for w in wants]),
        )
        out: dict[str, dict[str, Any]] = {}
        for r in cur.fetchall():
            # What was read from the pinned source wins over what the
            # registry's metadata says. The metadata can be stale or empty.
            observed = sorted(x for x in (r[5] or []) if x)
            if observed:
                license_, source = ", ".join(observed), "evidence"
            elif r[3]:
                license_, source = r[3], "metadata"
            else:
                license_, source = None, None
            out[str(r[0])] = {
                "capability_key": r[1], "display_name": r[2],
                "license": license_, "license_source": source, "cost_tier": r[4],
                "pinned_revision": r[6],
            }
        return out


def missing_evidence(
    conn, cv_ids: list[str], constraint_kinds: list[str],
) -> dict[str, list[str]]:
    """
    For each capability version, the kinds of evidence a fit matcher would
    need for this need's constraints and does not have. Empty list: the
    matchers have something to read, so a gap they report is a finding.

      license_allowlist     needs a license CIP identified (not a LICENSE
                            file it could not name, nor a low-confidence
                            mention)
      required_interface    needs interfaces read from the pinned revision
                            (evidence rows, linked to the version or
                            not: the matcher reads both), or a successful
                            interface-extraction stage that found none
      forbidden_dependency  the same for dependencies: dependency evidence
                            on the pinned revision, or a successful
                            'manifests' stage that found none

      (always)              a scorecard. The rules need an intrinsic
                            score; a candidate with none scores 0.0 and
                            matches no rule, which reads as "rejected"

    This is the same set of rows workers.recommend._load_candidate_inputs
    reads. The two have to agree: "CIP holds the evidence" is a statement
    about what the matcher was given. They once did not. Evidence that
    analysis wrote but promotion never linked counted here and was not
    read there, and CIP decided on an empty list it had called sufficient.

    All of it is read for the PINNED revision only: interface and
    dependency rows count through the evidence item they were extracted
    from, and that evidence item belongs to one revision.

    A component ingested from registry metadata has none of these. One
    that was analysed for something else (say, test presence) may still
    lack the evidence a particular constraint depends on, which is why
    this is judged per constraint kind and not as "was it analysed at all".
    """
    ids = [c for c in dict.fromkeys(cv_ids) if c]
    if not ids:
        return {}
    want = {
        "license": "license_allowlist" in constraint_kinds,
        "interfaces": "required_interface" in constraint_kinds,
        "dependencies": "forbidden_dependency" in constraint_kinds,
    }
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH pinned AS (
                SELECT cv.id AS cv_id,
                       (SELECT csb.source_revision_id
                          FROM capability_source_binding csb
                          JOIN source_revision sr ON sr.id = csb.source_revision_id
                         WHERE csb.capability_version_id = cv.id
                         ORDER BY {PINNED_REVISION_ORDER} LIMIT 1) AS rev_id
                FROM capability_version cv
                WHERE cv.id = ANY(%s::uuid[])
            )
            SELECT p.cv_id,
                   EXISTS (SELECT 1 FROM evidence_item ei
                            WHERE ei.source_revision_id = p.rev_id
                              AND {IDENTIFIED_LICENSE}) AS has_license,
                   (EXISTS (SELECT 1 FROM evidence_item ei
                             WHERE ei.source_revision_id = p.rev_id
                               AND ei.evidence_type = 'interface')
                    OR EXISTS (SELECT 1 FROM analysis_run ar
                                 JOIN analysis_stage_result asr
                                   ON asr.analysis_run_id = ar.id
                                WHERE ar.source_revision_id = p.rev_id
                                  AND asr.extractor_name IN ('interfaces', 'ts_interfaces')
                                  AND asr.status = 'succeeded')) AS has_interfaces,
                   (EXISTS (SELECT 1 FROM evidence_item ei
                             WHERE ei.source_revision_id = p.rev_id
                               AND ei.evidence_type = 'dependency')
                    OR EXISTS (SELECT 1 FROM analysis_run ar
                                 JOIN analysis_stage_result asr
                                   ON asr.analysis_run_id = ar.id
                                WHERE ar.source_revision_id = p.rev_id
                                  AND asr.extractor_name = 'manifests'
                                  AND asr.status = 'succeeded')) AS has_dependencies,
                   EXISTS (SELECT 1 FROM scorecard sc
                            WHERE sc.capability_version_id = p.cv_id) AS has_score
            FROM pinned p
            """,
            (ids,),
        )
        rows = cur.fetchall()
    out: dict[str, list[str]] = {}
    for cv_id, has_license, has_interfaces, has_dependencies, has_score in rows:
        have = {"license": has_license, "interfaces": has_interfaces,
                "dependencies": has_dependencies}
        missing = [k for k in ("license", "interfaces", "dependencies")
                   if want[k] and not have[k]]
        if not has_score:
            missing.append("score")
        out[str(cv_id)] = missing
    return out


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------

def load_active_lock(conn, project_id: str) -> dict[str, Any] | None:
    return _load_lock(conn, "project_id = %s AND status = 'active'", (project_id,))


def load_lock(conn, lock_id: str) -> dict[str, Any] | None:
    return _load_lock(conn, "id = %s", (lock_id,))


def _load_lock(conn, where: str, params: tuple) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, project_id, version, status, approved_by, approved_at,
                   approval_note, decisions, approved_providers, cost_commitments,
                   unresolved_risks, source_lock_hash, approval_id, payload_hash,
                   requirements_hash, architecture_doc_hash, architecture_doc_ref,
                   project_decisions, scope, replaces_version, architecture_doc_file_hash,
                   follows_request_id, requirements_approval_id
            FROM architecture_lock
            WHERE {where}
            """,
            params,
        )
        r = cur.fetchone()
    if not r:
        return None
    # A row this version did not write can hold anything in its JSONB
    # columns. Whatever is not the expected shape is read as empty and the
    # lock as unreadable, which every caller treats as stale.
    decisions, readable = r[7], True
    if isinstance(decisions, str):
        try:
            decisions = json.loads(decisions)
        except ValueError:
            decisions = None
    if not (isinstance(decisions, list) and all(
            isinstance(d, dict) and isinstance(d.get("need_slug"), str)
            and isinstance(d.get("verdict"), str) for d in decisions)):
        legacy_shape = r[13] is None or r[12] is None
        decisions, readable = (decisions if legacy_shape and isinstance(decisions, list)
                               else []), legacy_shape

    def as_list(v):
        return v if isinstance(v, list) else []

    return {
        "id": str(r[0]),
        "project_id": str(r[1]),
        "version": r[2],
        "status": r[3],
        "approved_by": r[4],
        "approved_at": r[5].isoformat(),
        "approval_note": r[6],
        "decisions": decisions,
        "approved_providers": as_list(r[8]),
        "cost_commitments": as_list(r[9]),
        "unresolved_risks": as_list(r[10]),
        "source_lock_hash": r[11],
        "approval_id": str(r[12]) if r[12] else None,
        "payload_hash": r[13],
        "requirements_hash": r[14],
        "architecture_doc_hash": r[15],
        "architecture_doc_ref": r[16],
        "project_decisions": as_list(r[17]),
        "scope": r[18] if isinstance(r[18], dict) else {},
        "replaces_version": r[19],
        "architecture_doc_file_hash": r[20],
        "follows_request_id": str(r[21]) if r[21] else None,
        "requirements_approval_id": str(r[22]) if r[22] else None,
        "is_legacy": r[13] is None or r[12] is None,
        "readable": readable,
    }


def lock_payload(lock: dict[str, Any]) -> dict[str, Any] | None:
    """Rebuild the hashed payload from a stored lock. None for a v1.0 lock."""
    if lock["is_legacy"]:
        return None
    return lock_rules.build_lock_payload(
        project_id=lock["project_id"],
        requirements_hash=lock["requirements_hash"],
        architecture_doc_hash=lock["architecture_doc_hash"],
        decisions=lock["decisions"],
        project_decisions=lock["project_decisions"],
        approved_providers=lock["approved_providers"],
        cost_commitments=lock["cost_commitments"],
        unresolved_risks=lock["unresolved_risks"],
        scope=lock["scope"],
        replaces_lock_version=lock["replaces_version"],
        architecture_doc_ref=lock["architecture_doc_ref"],
        architecture_doc_file_hash=lock["architecture_doc_file_hash"],
        follows_lock_request=lock["follows_request_id"],
        requirements_approval_id=lock["requirements_approval_id"],
    )


def lock_stale_reasons(
    lock: dict[str, Any], current_requirements_hash: str,
    confirmation_state: str,
) -> list[str]:
    """
    Reasons the active lock cannot be relied on. Empty means usable.

    Fail-closed on four things: no recorded human approval, content that
    no longer matches its approved hash, requirements that have moved
    since the lock, and requirements that are not confirmed as they stand.

    The last one is not implied by the third. Confirm scope A and lock it;
    add a requirement and have the human confirm B; then retire the
    addition. The requirements hash is A's again, so the old lock's hash
    matches — but the human's latest word was B, and A has not been
    confirmed since. Gate 1 is closed, and the lock must be closed with it.
    """
    if lock["is_legacy"]:
        return [
            f"lock v{lock['version']} was created before v1.1 with a free-text approver "
            f"and no approval record; relock to put it under a recorded human approval"
        ]
    if not lock["readable"]:
        return [
            f"lock v{lock['version']}: its stored decisions are not in a form this "
            f"version can read; relock"
        ]
    reasons: list[str] = []
    payload = lock_payload(lock)
    if lock_rules.payload_hash(payload) != lock["payload_hash"]:
        reasons.append(
            f"lock v{lock['version']}: stored content does not match its approved hash"
        )
    if lock["requirements_hash"] != current_requirements_hash:
        reasons.append(
            "the requirements have changed since the lock; reconfirm them and relock"
        )
    elif confirmation_state != "approved":
        reasons.append(
            "the requirements are not confirmed as they now stand (the latest "
            "confirmation was for different content); reconfirm them"
        )
    return reasons


def locked_decisions_by_slug(lock: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not lock or lock["is_legacy"]:
        return {}
    return {d["need_slug"]: d for d in lock["decisions"]}


# ---------------------------------------------------------------------------
# Tasks and verification
# ---------------------------------------------------------------------------

def load_tasks(conn, project_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bt.id, bt.architecture_lock_id, bt.title, bt.objective, bt.status,
                   bt.permitted_paths, bt.prohibited_changes,
                   bt.reused_capability_version_id, bt.target_commit,
                   bt.created_at, bt.updated_at,
                   COALESCE(array_agg(btr.project_requirement_id)
                            FILTER (WHERE btr.project_requirement_id IS NOT NULL), '{}')
            FROM build_task bt
            LEFT JOIN build_task_requirement btr ON btr.build_task_id = bt.id
            WHERE bt.project_id = %s
            GROUP BY bt.id
            ORDER BY bt.created_at, bt.id
            """,
            (project_id,),
        )
        rows = cur.fetchall()
        cur.execute(
            """
            SELECT payload->>'build_task_id', count(*)
            FROM audit_event
            WHERE project_id = %s AND event_type = 'lock_check'
              AND payload->>'build_task_id' IS NOT NULL
              -- A check that was refused as unreadable or as having nothing
              -- to check is not a check of anything.
              AND payload->>'verdict' IN ('allowed', 'needs_approval')
            GROUP BY 1
            """,
            (project_id,),
        )
        checks = {r[0]: r[1] for r in cur.fetchall()}
    return [
        {
            "id": str(r[0]),
            "architecture_lock_id": str(r[1]),
            "title": r[2],
            "objective": r[3],
            "status": r[4],
            "permitted_paths": list(r[5] or []),
            "prohibited_changes": list(r[6] or []),
            "reused_capability_version_id": str(r[7]) if r[7] else None,
            "target_commit": r[8],
            "created_at": r[9].isoformat(),
            "updated_at": r[10].isoformat(),
            "requirement_ids": [str(x) for x in r[11]],
            "lock_checks": checks.get(str(r[0]), 0),
        }
        for r in rows
    ]


def latest_verifications(
    conn, project_id: str, live_task_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """
    Latest verification that counts, per acceptance criterion, keyed by
    criterion id.

    "Latest" is insertion order (recorded_seq), not verified_at: several
    verifications reported in one call share a transaction timestamp.

    What counts:
      a pass    only while the task that recorded it is live (current and
                not abandoned). A pass filed by a task that was then
                abandoned, or that a relock made stale, is evidence about
                work that is no longer the build.
      a fail    always. Something was run and did not pass; abandoning
                the task that ran it does not make that untrue. (It used
                to: abandon the task that observed the failure, and the
                older pass underneath came back as the latest word.)

      not_run   as the latest word it means "unverified", and it replaces
                a pass. It does not replace a fail: "I did not run it" filed
                after a failure is not news about the failure. If the last
                run that had a result failed, the criterion is failing.

    There is one rule, and every caller passes the live task ids: the
    write path that grants `verified` and the reads that report it cannot
    then disagree.
    """
    def latest(extra: str) -> dict[str, dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT DISTINCT ON (rv.acceptance_criterion_id)
                       rv.acceptance_criterion_id, rv.outcome, rv.build_task_id,
                       rv.recorded_by, rv.commit_sha, rv.test_ref, rv.verified_at,
                       rv.command, rv.worktree_dirty, rv.evidence_ref
                FROM requirement_verification rv
                JOIN project_requirement pr ON pr.id = rv.project_requirement_id
                WHERE pr.project_id = %s AND rv.acceptance_criterion_id IS NOT NULL
                  AND (rv.outcome <> 'pass' OR rv.build_task_id = ANY(%s::uuid[]))
                  {extra}
                ORDER BY rv.acceptance_criterion_id, rv.recorded_seq DESC
                """,
                (project_id, list(live_task_ids)),
            )
            return {
                str(r[0]): {
                    "outcome": r[1],
                    "build_task_id": str(r[2]) if r[2] else None,
                    "recorded_by": r[3],
                    "commit_sha": r[4],
                    "test_ref": r[5],
                    "verified_at": r[6].isoformat(),
                    "command": r[7],
                    "worktree_dirty": r[8],
                    "evidence_ref": r[9],
                }
                for r in cur.fetchall()
            }

    overall = latest("")
    with_result = latest("AND rv.outcome <> 'not_run'")
    for criterion_id, row in overall.items():
        ran = with_result.get(criterion_id)
        if row["outcome"] == "not_run" and ran and ran["outcome"] == "fail":
            overall[criterion_id] = ran
    return overall


def dependent_need_slugs(task: dict[str, Any], scope_index: dict[str, dict]) -> list[str]:
    """
    Capability needs a task rests on: needs it links directly, plus the
    needs behind each via_needs requirement it links.
    """
    slugs: set[str] = set()
    for rid in task["requirement_ids"]:
        row = scope_index.get(rid)
        if not row:
            continue
        if row["kind"] == req_rules.NEED_KIND:
            slugs.add(row["slug"])
        else:
            slugs.update(row.get("need_slugs") or [])
    return sorted(slugs)


def task_is_current(
    task: dict[str, Any],
    *,
    active_lock: dict[str, Any] | None,
    task_lock_decisions: dict[str, dict[str, Any]],
    scope_index: dict[str, dict],
    task_lock_is_legacy: bool = False,
) -> bool:
    """
    A task is current when the decisions it was created under are still
    the locked ones. A task from an earlier lock version stays current if
    nothing it depends on changed; otherwise it is stale and can only be
    abandoned.
    """
    if not active_lock or active_lock["is_legacy"]:
        return False
    # A task made under a lock nobody approved was never authorised. A
    # later, approved lock does not adopt it.
    if task_lock_is_legacy:
        return False
    # Everything it links must still be an active row, and every
    # requirement it delivers must still be in the build.
    for rid in task["requirement_ids"]:
        row = scope_index.get(rid)
        if row is None:
            return False
        if (row["kind"] != req_rules.NEED_KIND
                and row.get("disposition") not in req_rules.IN_SCOPE_DISPOSITIONS):
            return False
    if task["architecture_lock_id"] == active_lock["id"]:
        return True
    active = locked_decisions_by_slug(active_lock)
    for slug in dependent_need_slugs(task, scope_index):
        then, now = task_lock_decisions.get(slug), active.get(slug)
        if not then or not now:
            return False
        if then.get("decision_record_id") != now.get("decision_record_id"):
            return False
    return True


def scope_index(scope: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Active rows keyed by id, with kind filled in for needs."""
    index: dict[str, dict[str, Any]] = {}
    for r in scope["requirements"]:
        index[r["id"]] = r
    for n in scope["needs"]:
        index[n["id"]] = {**n, "kind": req_rules.NEED_KIND}
    return index
