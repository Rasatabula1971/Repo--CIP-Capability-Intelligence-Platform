"""
PDR-to-Build workflow — tool implementations (Implementation Spec v1.1).

The driving LLM reads the PDR, designs, and writes the code. CIP is the
system of record, the judgment and the gate. The tools, in the order a
build uses them:

  pdr_status                         where the project is; what is allowed next
  ingest_pdr                         store the PDR text as the provenance root
  record_requirements                PDR requirements, each anchored to a quote
  record_capability_needs            the reusable capabilities the build needs
  request_requirements_confirmation  HUMAN GATE 1 — confirm the scope
  search_for_requirement             registry + evaluation-store search, logged
  record_decision                    CIP computes; the LLM may propose
  build_approval_brief               preview what the lock will freeze (view)
  lock_architecture                  HUMAN GATE 2 — freeze approved decisions
  record_build_progress              tasks, status, verification evidence
  check_against_lock                 test a change against the lock (logged)
  coverage                           how much of the PDR is built, on what evidence
  retire_requirement                 take a requirement or need out of scope

Rules live in core.pdr (pure). Fact loading lives in mcp_server.pdr_store.
This module sequences them and owns the transactions.

Return conventions (unchanged from v1.0 so existing callers keep working):
  None                      the id is malformed or the row does not exist
  {"error": ...}            bad input
  {"refused": True, ...}    a gate said no; `reason` says why and what to do

Every gate refusal is written to audit_event.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

import psycopg

from core.pdr import coverage as coverage_rules
from core.pdr import decisions as decision_rules
from core.pdr import lock as lock_rules
from core.pdr import lock_check as lock_check_rules
from core.pdr import phase as phase_rules
from core.pdr import requirements as req_rules
from core.pdr import tasks as task_rules
from core.pdr.canonical import normalise_text, text_hash
from core.pdr.spans import MAX_QUOTE_CHARS, QuoteLocator
from core.pdr.text import first_bad, is_blank, is_plain_line, one_line
from core.policy.build_gate import BuildGateError, authorize_build, can_generate_for
from core.project.fit import evaluate_fit
from core.project.types import CandidateInputs, Constraint, FitResult, Requirement
from mcp_server import pdr_approvals
from mcp_server import pdr_store as store
from mcp_server.queries import _filter_by_constraints, search_capabilities

log = logging.getLogger(__name__)

MAX_PDR_BYTES = 2_000_000
MAX_DOC_BYTES = 2_000_000
# What ingest_pdr and lock_architecture will read from disk. The server
# reads the file with its own permissions, so "any path" would make these
# tools a way to read arbitrary files; documents only.
DOCUMENT_SUFFIXES = (".md", ".markdown", ".txt", ".rst", ".text")
# The mandatory sweep takes up to this many hits per term, whatever limit
# the caller passes (it is also the most search_capabilities returns). It
# is a ceiling on work, not a quota: if a term returns this many, the
# search is recorded as truncated, and "nothing fits" cannot be concluded
# from it.
SWEEP_LIMIT = 100
MAX_TITLE_CHARS = 120
# A project name is printed in headings and status lines beside facts the
# reader acts on. Short enough that it cannot fill a terminal row and push
# text of its own onto the next.
MAX_PROJECT_NAME_CHARS = 60
MAX_TASK_TITLE_CHARS = 120
MAX_RATIONALE_CHARS = 2000
MAX_ITEMS_PER_CALL = 500

_is_uuid = store.is_uuid
_canon = store.canon_uuid


def _tool(fn):
    """
    Wrap a public tool function so that a collision with a concurrent call
    is an answer, not a traceback. Two sessions writing the same lock or
    opening the same request race for a unique key; the loser's
    transaction is rolled back, the state is whatever the winner wrote,
    and the caller is told to look again.
    """
    import functools

    @functools.wraps(fn)
    def wrapper(conn, *args, **kwargs):
        try:
            return fn(conn, *args, **kwargs)
        except (psycopg.errors.UniqueViolation,
                psycopg.errors.SerializationFailure,
                psycopg.errors.DeadlockDetected) as e:
            conn.rollback()
            log.warning("%s: concurrent write (%s)", fn.__name__, type(e).__name__)
            return {
                "error": "another call changed this project at the same moment, so "
                         "nothing from this call was kept. Call pdr_status and try again.",
                "retry": True,
            }
    return wrapper


def _one_at_a_time(conn, project_id: str) -> None:
    """
    Hold a per-project lock until this transaction ends. A second call for
    the same project waits here, and then reads what the first one wrote.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"cip-pdr-lock:{project_id}",))


def _refuse(reason: str, **extra: Any) -> dict[str, Any]:
    return {"refused": True, "reason": reason, **extra}


def _gate_refusal(
    conn, project_id: str | None, tool: str, reason: str, **extra: Any,
) -> dict[str, Any]:
    """
    A refusal that is recorded. Discards anything the call had written so
    far, then logs the refusal on its own, so a refused call leaves an
    audit row and nothing else.
    """
    conn.rollback()
    store.audit(
        conn, project_id=project_id, event_type="gate_refused",
        payload={"tool": tool, "reason": reason, "gate": extra.get("gate")},
    )
    conn.commit()
    return _refuse(reason, **extra)


def _bad_input(*values: Any) -> dict[str, Any] | None:
    reason = first_bad(*values)
    return {"error": f"invalid input: {reason}"} if reason else None


# ---------------------------------------------------------------------------
# Reading documents from disk
# ---------------------------------------------------------------------------

def _read_document(path: Any, limit: int, what: str) -> tuple[str | None, str | None, str | None]:
    """
    Read a text document from the machine the server runs on.

    Returns (text, sha256 of the raw file bytes, error). The path must be
    absolute: the server's working directory is CIP's own folder, so a
    relative path would quietly resolve to the wrong file.
    """
    if not isinstance(path, str) or not path.strip():
        return None, None, f"{what} path must be a non-empty string"
    raw = path.strip()
    # Absolute by this machine's own rules. On Windows that means a drive
    # or a UNC share: "/Users/x/pdr.md" has neither and would resolve
    # against whatever drive the server happens to be running from.
    if not Path(raw).is_absolute():
        return None, None, (
            f"{what} path must be absolute (got '{one_line(raw, 200)}'). The server "
            f"does not run in your project folder, so a relative path would point "
            f"somewhere else."
        )
    p = Path(raw)
    if p.suffix.lower() not in DOCUMENT_SUFFIXES:
        return None, None, (
            f"{what} must be a text document ({', '.join(DOCUMENT_SUFFIXES)}); "
            f"got '{p.suffix or 'no extension'}'"
        )
    configured = os.environ.get("CIP_DOCUMENT_ROOTS")
    roots = [r.strip() for r in (configured or "").split(os.pathsep) if r.strip()]
    if configured is not None and not roots:
        # Set, but naming no folder. Someone meant to confine the reader;
        # treating that as "no confinement" would be the wrong way to fail.
        return None, None, (
            "CIP_DOCUMENT_ROOTS is set but names no folder, so no document can be read. "
            "List the folders CIP may read from, or unset it."
        )
    try:
        resolved = p.resolve()
        if roots and not any(_is_under(resolved, Path(r).expanduser().resolve()) for r in roots):
            return None, None, f"{what} is outside the folders allowed by CIP_DOCUMENT_ROOTS"
        if not resolved.is_file():
            return None, None, f"{what} not found: {raw}"
        if resolved.stat().st_size > limit:
            return None, None, f"{what} is larger than {limit // 1000} KB"
        data = resolved.read_bytes()
    except OSError as e:
        return None, None, f"could not read {what}: {e}"

    # PowerShell 5.1 redirection writes UTF-16 with a BOM.
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encoding = "utf-16"
    else:
        encoding = "utf-8-sig"
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        return None, None, f"{what} is not UTF-8 (or UTF-16 with a byte-order mark) text"
    return text, hashlib.sha256(data).hexdigest(), None


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Candidate loading + fit persistence
# ---------------------------------------------------------------------------

def _load_candidate_inputs(
    conn: psycopg.Connection, cv_id: str,
) -> CandidateInputs | None:
    """
    Everything core.project.fit needs about a capability version, or None
    when it has no source binding and so cannot be pinned to a revision.

    One loader, the Step 12 worker's. This module had a copy of its own,
    and the rule "evidence is read for the pinned revision" was applied to
    one copy and not the other: the decision judged a candidate on one
    set of interfaces and the search displayed it on another.
    """
    from workers.recommend import _load_candidate_inputs as load
    return load(conn, uuid.UUID(cv_id))


def _persist_fit_evaluation(
    conn: psycopg.Connection,
    *,
    project_requirement_id: str,
    candidate: CandidateInputs,
    fit_result: FitResult,
) -> str:
    """Upsert one fit evaluation with its gaps and evidence links (the Step 12 writer)."""
    from workers.recommend import _persist_fit_evaluation as persist
    return str(persist(
        conn, project_requirement_id=uuid.UUID(project_requirement_id),
        candidate=candidate, fit_result=fit_result,
    ))


# Which kind of evidence each blocking fit gap rests on.
_GAP_EVIDENCE = {
    "license_mismatch": "license",
    "forbidden_dependency_present": "dependencies",
    "missing_interface": "interfaces",
}


def _read_fit_evaluations(
    conn: psycopg.Connection,
    req_id: str,
    cv_ids: list[str] | None,
    constraint_kinds: list[str],
) -> list[dict[str, Any]]:
    """
    Fit rows for a need, best first. Restricted to cv_ids when given.

    Each candidate is marked with whether the fit matchers had evidence
    to read for this need's constraints. Where they did not, its fit
    score and gap count say what is unknown, not what is wrong.
    """
    if cv_ids is not None and not cv_ids:
        return []
    sql = """
        SELECT fe.id, fe.capability_version_id, fe.fit_score,
               fe.blocking_gap_count, fe.computed_hash,
               cv.display_version,
               c.normalized_key, c.display_name, c.ecosystem,
               c.component_kind, c.runtime, c.cost_tier, c.license_spdx,
               (SELECT MAX(sc.total_score) FROM scorecard sc
                 WHERE sc.capability_version_id = cv.id)
        FROM fit_evaluation fe
        JOIN capability_version cv ON cv.id = fe.capability_version_id
        JOIN capability c ON c.id = cv.capability_id
        WHERE fe.project_requirement_id = %s
    """
    params: list[Any] = [req_id]
    if cv_ids is not None:
        sql += " AND fe.capability_version_id = ANY(%s::uuid[])"
        params.append(cv_ids)
    sql += (" ORDER BY fe.blocking_gap_count, fe.fit_score DESC, 14 DESC NULLS LAST, "
            "fe.capability_version_id")
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.execute(
            "SELECT fit_evaluation_id, kind FROM fit_gap "
            "WHERE fit_evaluation_id = ANY(%s::uuid[]) AND is_blocking",
            ([r[0] for r in rows],),
        )
        blocking_kinds: dict[str, list[str]] = {}
        for fid, gap_kind in cur.fetchall():
            blocking_kinds.setdefault(str(fid), []).append(gap_kind)
    cv_ids_found = [str(r[1]) for r in rows]
    missing = store.missing_evidence(conn, cv_ids_found, constraint_kinds)
    facts = store.capability_facts(conn, cv_ids_found)

    def confirmed(fit_id: str, cv_id: str) -> list[str]:
        # A blocking gap is a finding only where CIP read the thing the gap
        # is about. "No license identified" on a repo nobody opened is
        # missing evidence; "GPL-3.0, not on the allowlist" is a finding.
        lacks = set(missing.get(cv_id, []))
        return sorted(k for k in blocking_kinds.get(fit_id, [])
                      if _GAP_EVIDENCE.get(k) not in lacks)

    return [
        {
            "fit_evaluation_id": str(r[0]),
            "capability_version_id": str(r[1]),
            "fit_score": float(r[2]),
            "blocking_gap_count": r[3],
            "confirmed_blocking_gaps": confirmed(str(r[0]), str(r[1])),
            "display_version": r[5],
            "normalized_key": r[6],
            "display_name": r[7],
            "ecosystem": r[8],
            "component_kind": r[9],
            "runtime": r[10],
            "cost_tier": r[11],
            "license": (facts.get(str(r[1])) or {}).get("license") or r[12],
            "license_source": (facts.get(str(r[1])) or {}).get("license_source"),
            "intrinsic_score": float(r[13]) if r[13] is not None else None,
            "evidence": "insufficient" if missing.get(str(r[1])) else "sufficient",
            "missing_evidence": missing.get(str(r[1]), []),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 1. ingest_pdr
# ---------------------------------------------------------------------------

@_tool
def ingest_pdr(
    conn: psycopg.Connection,
    *,
    project_name: str,
    pdr_title: str,
    pdr_text: str | None = None,
    pdr_path: str | None = None,
    project_id: str | None = None,
    stack: str | None = None,
    policy_profile: str | None = None,
) -> dict[str, Any]:
    """
    Register the project and store the PDR as the provenance root.

    Give pdr_path (absolute) where the server can read the file: passing a
    long document through a tool argument costs tokens, lets the text
    drift from what is on disk, and leaves nothing for the human to check
    the stored text against. pdr_text stays for short documents and tests,
    and is reported to the human as "supplied inline".

    The text is stored so every requirement's quote can be located and
    re-checked. Ingesting a different text makes it the current PDR;
    requirements whose quotes still appear are carried forward, the rest
    are reported as orphaned. Ingesting an earlier text again makes that
    one current again.
    """
    bad = _bad_input(project_name, pdr_title, pdr_text, pdr_path, stack, policy_profile)
    if bad:
        return bad
    if not isinstance(pdr_title, str) or not one_line(pdr_title):
        return {"error": "pdr_title is required"}
    if len(pdr_title) > MAX_TITLE_CHARS:
        return {"error": f"pdr_title is longer than {MAX_TITLE_CHARS} characters"}
    if isinstance(project_name, str) and len(project_name) > MAX_PROJECT_NAME_CHARS:
        return {"error": f"project_name is longer than {MAX_PROJECT_NAME_CHARS} characters"}
    # Names are printed in every status line and summary heading. They are
    # single lines of visible text, or they are refused.
    for label, value in (("pdr_title", pdr_title), ("project_name", project_name)):
        if isinstance(value, str) and not is_plain_line(value):
            return {"error": f"{label} must be plain single-line text"}
    for label, value in (("project_id", project_id), ("stack", stack),
                         ("policy_profile", policy_profile)):
        if value is not None and not isinstance(value, str):
            return {"error": f"{label} must be a string"}
    if (pdr_text is None) == (pdr_path is None):
        return {"error": "provide exactly one of pdr_path or pdr_text"}

    file_hash = None
    raw_ref = "inline"
    if pdr_path is not None:
        pdr_text, file_hash, err = _read_document(pdr_path, MAX_PDR_BYTES, "PDR file")
        if err:
            return {"error": err}
        unstorable = first_bad(pdr_text)
        if unstorable:
            return {"error": f"the PDR file cannot be stored: {unstorable}. If it was saved "
                             f"as UTF-16 without a byte-order mark, save it as UTF-8."}
        raw_ref = str(Path(pdr_path.strip()).expanduser())
    elif not isinstance(pdr_text, str):
        return {"error": "pdr_text must be a string"}
    if len(pdr_text.encode("utf-8")) > MAX_PDR_BYTES:
        return {"error": f"PDR is larger than {MAX_PDR_BYTES // 1000} KB"}

    text = normalise_text(pdr_text)
    if not text.strip():
        return {"error": "the PDR is empty"}
    content_hash = text_hash(text)

    with conn.cursor() as cur:
        if project_id:
            wanted = _canon(project_id)
            if not wanted:
                return {"error": f"Project {one_line(project_id, 60)} not found"}
            cur.execute("SELECT id FROM project WHERE id = %s", (wanted,))
            row = cur.fetchone()
            if not row:
                return {"error": f"Project {wanted} not found"}
            pid = str(row[0])
        else:
            if not isinstance(project_name, str) or not project_name.strip():
                return {"error": "project_name is required"}
            cur.execute(
                """
                INSERT INTO project (name, metadata)
                VALUES (%s, %s::jsonb)
                ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
                RETURNING id
                """,
                (project_name.strip(), json.dumps({
                    "stack": stack,
                    "policy_profile": policy_profile,
                })),
            )
            pid = str(cur.fetchone()[0])

    prior = store.latest_source(conn, pid)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, raw_text FROM requirement_source "
            "WHERE project_id = %s AND content_hash = %s",
            (pid, content_hash),
        )
        existing = cur.fetchone()

    if existing and prior and prior["id"] == str(existing[0]):
        # Same text as the current PDR: nothing changes. A v1.0 source
        # stored the hash only; same hash, so keep the text now.
        # Text first supplied inline and now read from a file with the same
        # content: record the file, so the human has something to check.
        # Never the other way round — a file reference is not dropped
        # because the same text was later pasted in.
        gains_file = raw_ref != "inline" and prior.get("raw_ref") in (None, "inline")
        if existing[1] is None or gains_file:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE requirement_source SET raw_text = %s, raw_ref = %s, "
                    "source_file_hash = %s WHERE id = %s",
                    (text, raw_ref, file_hash, existing[0]),
                )
        conn.commit()
        return {
            "project_id": pid,
            "requirement_source_id": str(existing[0]),
            "content_hash": content_hash,
            "was_duplicate": True,
            "is_current": True,
        }

    with conn.cursor() as cur:
        if existing:
            # An earlier text is being brought back. Re-activate its row.
            rs_id = str(existing[0])
            cur.execute(
                "UPDATE requirement_source SET activated_at = clock_timestamp(), "
                "raw_text = %s, raw_ref = %s, source_file_hash = %s WHERE id = %s",
                (text, raw_ref, file_hash, rs_id),
            )
        else:
            cur.execute(
                """
                INSERT INTO requirement_source
                    (project_id, title, doc_kind, content_hash, raw_text,
                     supersedes_id, raw_ref, source_file_hash, activated_at)
                VALUES (%s, %s, 'pdr', %s, %s, %s, %s, %s, clock_timestamp())
                RETURNING id
                """,
                (pid, pdr_title.strip(), content_hash, text,
                 prior["id"] if prior else None, raw_ref, file_hash),
            )
            rs_id = str(cur.fetchone()[0])

        carried: list[str] = []
        orphaned: list[str] = []
        if prior:
            cur.execute(
                """
                SELECT id, slug, source_span
                FROM project_requirement
                WHERE project_id = %s AND status = 'active'
                  AND kind <> 'capability_need'
                  AND requirement_source_id IS NOT NULL
                """,
                (pid,),
            )
            locator = QuoteLocator(text)
            for rid, slug, span in cur.fetchall():
                span = span if isinstance(span, dict) else {}
                quote = span.get("quote") if isinstance(span.get("quote"), str) else None
                hint = span.get("start") if isinstance(span.get("start"), int) else None
                found = locator.locate(quote, hint) if quote else None
                # Carried forward only if the quote would be accepted against
                # THIS text. A quote recorded against a flattened copy of the
                # PDR, then carried onto the real one, spanned five sections
                # and mapped them all.
                if (found is None or found.end - found.start > MAX_QUOTE_CHARS
                        or locator.heading_inside(found) is not None):
                    orphaned.append(slug)
                    continue
                cur.execute(
                    "UPDATE project_requirement "
                    "SET requirement_source_id = %s, source_span = %s::jsonb "
                    "WHERE id = %s",
                    (rs_id, json.dumps({
                        "start": found.start, "end": found.end,
                        "quote": text[found.start:found.end],
                    }), rid),
                )
                carried.append(slug)

    store.audit(
        conn, project_id=pid, event_type="pdr_ingested",
        subject_table="requirement_source", subject_id=rs_id,
        payload={"content_hash": content_hash, "source": raw_ref,
                 "source_file_hash": file_hash,
                 "replaces_source_id": prior["id"] if prior else None,
                 "reactivated": bool(existing),
                 "carried_forward": len(carried), "orphaned": sorted(orphaned)},
    )
    conn.commit()
    out: dict[str, Any] = {
        "project_id": pid,
        "requirement_source_id": rs_id,
        "content_hash": content_hash,
        "was_duplicate": bool(existing),
        "is_current": True,
        "supersedes_id": prior["id"] if prior else None,
    }
    if prior:
        out["carried_forward"] = sorted(carried)
        out["orphaned"] = sorted(orphaned)
        out["note"] = (
            "This text is now the current PDR, replacing the previous one. "
            "Requirements whose quotes still appear, inside one section, were carried "
            "forward; orphaned ones must be re-recorded or retired. The requirements must be confirmed "
            "again, and an existing lock is stale until relocked."
        )
    return out


# ---------------------------------------------------------------------------
# 2. record_requirements
# ---------------------------------------------------------------------------

@_tool
def record_requirements(
    conn: psycopg.Connection,
    *,
    project_id: str,
    requirement_source_id: str,
    requirements: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Record requirements extracted from the PDR.

    Each needs a verbatim `quote`; CIP finds it in the stored text and
    computes the span. Offsets supplied by the caller are ignored, and a
    quote that is not in the PDR is refused.

    Passing an explicit `slug` updates that requirement. Without one the
    slug is derived from the text, and a derived slug that lands on a
    different existing requirement is refused rather than overwriting it.
    """
    project_id, requirement_source_id = _canon(project_id), _canon(requirement_source_id)
    if not project_id or not requirement_source_id:
        return {"error": "Invalid UUID"}
    if not isinstance(requirements, list):
        return {"error": "requirements must be a list"}
    if len(requirements) > MAX_ITEMS_PER_CALL:
        return {"error": f"at most {MAX_ITEMS_PER_CALL} requirements per call"}
    bad = _bad_input(requirements)
    if bad:
        return bad

    project = store.get_project(conn, project_id)
    if not project:
        return {"error": f"Project {project_id} not found"}

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, raw_text FROM requirement_source WHERE id = %s AND project_id = %s",
            (requirement_source_id, project_id),
        )
        src = cur.fetchone()
    if not src:
        return {"error": f"Requirement source {requirement_source_id} not found"}
    current = store.latest_source(conn, project_id)
    if current and current["id"] != str(src[0]):
        return {
            "error": "that requirement source is not the current PDR; "
                     f"record against {current['id']}",
        }
    pdr_text = src[1]
    locator = QuoteLocator(pdr_text) if pdr_text is not None else None

    results: list[dict[str, Any]] = []
    evidence_dropped: list[dict[str, Any]] = []
    seen_in_batch: dict[str, int] = {}
    changed = False
    for i, raw in enumerate(requirements):
        clean, errors = req_rules.validate_requirement_draft(raw, pdr_text, locator)
        if clean is None:
            results.append({"index": i, "valid": False, "errors": errors})
            continue
        slug = clean["slug"]
        if slug in seen_in_batch:
            results.append({
                "index": i, "slug": slug, "valid": False,
                "errors": [
                    f"slug '{slug}' is the same as item {seen_in_batch[slug]} in this call; "
                    f"give each requirement its own explicit slug"
                ],
            })
            continue

        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, kind, source_span, description FROM project_requirement "
                "WHERE project_id = %s AND slug = %s",
                (project_id, slug),
            )
            existing = cur.fetchone()
            if existing and existing[1] == req_rules.NEED_KIND:
                results.append({
                    "index": i, "slug": slug, "valid": False,
                    "errors": [f"slug '{slug}' is already a capability need"],
                })
                continue
            if existing and not clean["slug_explicit"]:
                # No slug was given, so nothing says this is meant as an
                # update. Overwriting on a guess is how a requirement's
                # text, priority and criteria change without anyone asking.
                results.append({
                    "index": i, "slug": slug, "valid": False,
                    "errors": [
                        f"the slug derived from this text ('{slug}') already belongs to a "
                        f"requirement. To update that requirement, pass slug='{slug}'; "
                        f"if this is a different one, give it its own slug."
                    ],
                })
                continue
            if existing and clean["disposition"] is not None:
                cur.execute(
                    """
                    SELECT n.slug FROM requirement_need_link l
                    JOIN project_requirement n ON n.id = l.need_id
                    WHERE l.requirement_id = %s AND n.status = 'active'
                    ORDER BY n.slug
                    """,
                    (existing[0],),
                )
                linked = [r[0] for r in cur.fetchall()]
                if linked:
                    results.append({
                        "index": i, "slug": slug, "valid": False,
                        "errors": [
                            f"cannot mark '{slug}' {clean['disposition']}: "
                            f"capability needs are linked to it ({', '.join(linked)}). "
                            f"Re-record those needs without it, or retire them, first."
                        ],
                    })
                    continue

            span_json = json.dumps({**clean["span"], "quote": clean["quote"]})
            set_disposition = clean["disposition"] is not None
            cur.execute(
                """
                INSERT INTO project_requirement
                    (project_id, slug, description, requirement_source_id,
                     source_span, priority, acceptance_criteria, kind,
                     disposition, disposition_reason)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
                ON CONFLICT (project_id, slug) DO UPDATE SET
                    description = EXCLUDED.description,
                    requirement_source_id = EXCLUDED.requirement_source_id,
                    source_span = EXCLUDED.source_span,
                    priority = EXCLUDED.priority,
                    acceptance_criteria = EXCLUDED.acceptance_criteria,
                    kind = EXCLUDED.kind,
                    status = 'active',
                    retired_reason = NULL,
                    disposition = CASE WHEN %s THEN EXCLUDED.disposition
                                       ELSE project_requirement.disposition END,
                    disposition_reason = CASE WHEN %s THEN EXCLUDED.disposition_reason
                                              ELSE project_requirement.disposition_reason END
                RETURNING id
                """,
                (
                    project_id, slug, clean["text"], requirement_source_id,
                    span_json, clean["priority"],
                    "\n".join(clean["acceptance_criteria"]) or None,
                    clean["kind"], clean["disposition"], clean["disposition_reason"],
                    set_disposition, set_disposition,
                ),
            )
            req_id = str(cur.fetchone()[0])
            # Evidence is for a criterion OF a requirement. If the
            # requirement itself now says something else — new wording, a
            # different part of the PDR, another kind — a criterion that
            # happens to keep its words is not the same promise, and a
            # pass recorded against it does not carry over.
            old_span = existing[2] if existing and isinstance(existing[2], dict) else {}
            rewritten = bool(existing) and (
                existing[3] != clean["text"]
                or existing[1] != clean["kind"]
                or old_span.get("quote") != clean["quote"]
            )
            # A FAIL is different. Something was run and did not pass, and
            # rewording the requirement does not make that untrue: deleting
            # the criteria took CIP-observed fails with them, and changing
            # the text and changing it back (the confirmed hash returns, no
            # human involved) was a way to erase one. So a rewrite drops
            # what is not a fail, and a criterion row goes only when its
            # own text does.
            dropped = []
            if rewritten:
                cur.execute(
                    """
                    DELETE FROM requirement_verification rv
                    USING acceptance_criterion ac
                    WHERE rv.acceptance_criterion_id = ac.id
                      AND ac.project_requirement_id = %s AND rv.outcome <> 'fail'
                    RETURNING ac.text, rv.outcome, rv.recorded_by, rv.commit_sha,
                              rv.command, rv.build_task_id::text
                    """,
                    (req_id,),
                )
                dropped += cur.fetchall()
            dropped += _sync_criteria(cur, req_id, clean["acceptance_criteria"])
            if dropped:
                evidence_dropped.append({"slug": slug, "rows": [
                    {"criterion": one_line(r[0], 200), "outcome": r[1], "recorded_by": r[2],
                     "commit_sha": r[3], "command": one_line(r[4], 200), "build_task_id": r[5]}
                    for r in dropped[:50]], "count": len(dropped)})

        seen_in_batch[slug] = i
        changed = True
        results.append({
            "index": i,
            "req_id": req_id,
            "slug": slug,
            "valid": True,
            "errors": [],
            "span": clean["span"],
        })

    if changed:
        _demote_lapsed_tasks(conn, project_id)
        store.audit(
            conn, project_id=project_id, event_type="requirements_recorded",
            payload={"slugs": [r["slug"] for r in results if r["valid"]]},
        )
    for item in evidence_dropped:
        # Evidence rows are deleted with the criterion they were about.
        # The audit trail keeps what they said.
        store.audit(
            conn, project_id=project_id, event_type="verification_evidence_dropped",
            payload=item,
        )
    conn.commit()

    scope = store.load_scope(conn, project_id)
    _, req_hash = store.scope_hash(scope, current)
    cov = store.scope_span_coverage(scope, current)
    return {
        "project_id": project_id,
        "requirement_source_id": requirement_source_id,
        "requirements": results,
        "requirements_hash": req_hash,
        "span_coverage": _coverage_brief(cov),
        "next": "Derive capability needs from the architecture sketch with "
                "record_capability_needs, mark the rest bespoke / not_applicable / "
                "deferred, then request_requirements_confirmation.",
    }


def _sync_criteria(cur, req_id: str, texts: list[str]) -> list[tuple]:
    """
    Make the requirement's criteria rows match `texts`, keeping the row
    (and so its verification history) for any criterion whose text is
    unchanged. Returns the verification rows that went with the criteria
    removed: (criterion text, outcome, recorded_by, commit, command, task).
    """
    cur.execute(
        "SELECT id, text FROM acceptance_criterion WHERE project_requirement_id = %s",
        (req_id,),
    )
    by_text = {text: cid for cid, text in cur.fetchall()}
    keep = {by_text[t] for t in texts if t in by_text}
    dropped: list[tuple] = []
    for text, cid in by_text.items():
        if cid not in keep:
            cur.execute(
                "SELECT %s, outcome, recorded_by, commit_sha, command, build_task_id::text "
                "FROM requirement_verification WHERE acceptance_criterion_id = %s "
                "ORDER BY recorded_seq",
                (text, cid),
            )
            dropped += cur.fetchall()
            cur.execute("DELETE FROM acceptance_criterion WHERE id = %s", (cid,))
    for ordinal, text in enumerate(texts, start=1):
        if text in by_text:
            cur.execute(
                "UPDATE acceptance_criterion SET ordinal = %s WHERE id = %s",
                (ordinal, by_text[text]),
            )
        else:
            cur.execute(
                "INSERT INTO acceptance_criterion (project_requirement_id, ordinal, text) "
                "VALUES (%s, %s, %s)",
                (req_id, ordinal, text),
            )
    return dropped


def _coverage_brief(cov: dict[str, Any] | None) -> dict[str, Any] | None:
    if cov is None:
        return None
    return {
        "section_count": cov["section_count"],
        "mapped_section_count": cov["mapped_section_count"],
        "unmapped_sections": cov["unmapped_sections"],
        "out_of_scope_only_sections": cov["out_of_scope_only_sections"],
        "lightly_quoted_sections": cov["lightly_quoted_sections"],
        "quoted_char_pct": cov["quoted_char_pct"],
    }


# ---------------------------------------------------------------------------
# 3. record_capability_needs
# ---------------------------------------------------------------------------

@_tool
def record_capability_needs(
    conn: psycopg.Connection,
    *,
    project_id: str,
    needs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """
    Record the reusable capabilities the build needs and link each to the
    PDR requirements it serves.

    A capability need is the unit that is searched and decided. Linking a
    need to a requirement sets that requirement's disposition to
    via_needs. Re-recording a need replaces its constraints and links.
    """
    project = store.get_project(conn, project_id)
    if not project:
        return None
    project_id = project["id"]
    if not isinstance(needs, list):
        return {"error": "needs must be a list"}
    if len(needs) > MAX_ITEMS_PER_CALL:
        return {"error": f"at most {MAX_ITEMS_PER_CALL} needs per call"}
    bad = _bad_input(needs)
    if bad:
        return bad

    results: list[dict[str, Any]] = []
    seen_in_batch: dict[str, int] = {}
    changed = False
    for i, raw in enumerate(needs):
        clean, errors, warnings = req_rules.validate_need_draft(raw)
        if clean is None:
            results.append({"index": i, "valid": False, "errors": errors})
            continue
        if clean["slug"] in seen_in_batch:
            results.append({
                "index": i, "slug": clean["slug"], "valid": False,
                "errors": [f"slug '{clean['slug']}' is the same as item "
                           f"{seen_in_batch[clean['slug']]} in this call"],
            })
            continue

        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, slug, kind, status FROM project_requirement WHERE project_id = %s",
                (project_id,),
            )
            rows = cur.fetchall()
            by_slug = {r[1]: r for r in rows}
            by_id = {str(r[0]): r for r in rows}

            item_errors: list[str] = []
            existing = by_slug.get(clean["slug"])
            if existing and existing[2] != req_rules.NEED_KIND:
                item_errors.append(
                    f"slug '{clean['slug']}' is already a PDR requirement; "
                    f"give the need its own slug"
                )
            elif existing and not clean["slug_explicit"]:
                item_errors.append(
                    f"the slug derived from this description ('{clean['slug']}') already "
                    f"belongs to a need. To update that need, pass slug='{clean['slug']}'; "
                    f"if this is a different one, give it its own slug."
                )

            target_ids: list[str] = []
            for ref in clean["satisfies"]:
                row = by_slug.get(ref) or by_id.get(ref)
                if row is None:
                    item_errors.append(f"satisfies: no requirement '{ref}' in this project")
                elif row[2] == req_rules.NEED_KIND:
                    item_errors.append(f"satisfies: '{ref}' is a capability need, not a requirement")
                elif row[3] != "active":
                    item_errors.append(f"satisfies: requirement '{ref}' is retired")
                else:
                    target_ids.append(str(row[0]))

            if item_errors:
                results.append({"index": i, "slug": clean["slug"], "valid": False,
                                "errors": item_errors})
                continue

            cur.execute(
                """
                INSERT INTO project_requirement
                    (project_id, slug, description, kind, search_terms, problem_types)
                VALUES (%s, %s, %s, 'capability_need', %s, %s)
                ON CONFLICT (project_id, slug) DO UPDATE SET
                    description = EXCLUDED.description,
                    search_terms = EXCLUDED.search_terms,
                    problem_types = EXCLUDED.problem_types,
                    status = 'active',
                    retired_reason = NULL
                RETURNING id
                """,
                (project_id, clean["slug"], clean["description"],
                 clean["search_terms"], clean["problem_types"]),
            )
            need_id = str(cur.fetchone()[0])

            cur.execute(
                "DELETE FROM requirement_constraint WHERE project_requirement_id = %s",
                (need_id,),
            )
            for c in clean["constraints"]:
                cur.execute(
                    "INSERT INTO requirement_constraint (project_requirement_id, kind, detail) "
                    "VALUES (%s, %s, %s::jsonb)",
                    (need_id, c["kind"], json.dumps(c["detail"])),
                )

            cur.execute("DELETE FROM requirement_need_link WHERE need_id = %s", (need_id,))
            for rid in dict.fromkeys(target_ids):
                cur.execute(
                    "INSERT INTO requirement_need_link (requirement_id, need_id) VALUES (%s, %s)",
                    (rid, need_id),
                )
            cur.execute(
                "UPDATE project_requirement "
                "SET disposition = 'via_needs', disposition_reason = NULL "
                "WHERE id = ANY(%s::uuid[])",
                (target_ids,),
            )

        seen_in_batch[clean["slug"]] = i
        changed = True
        results.append({
            "index": i,
            "need_id": need_id,
            "slug": clean["slug"],
            "valid": True,
            "errors": [],
            "warnings": warnings,
            "satisfies": sorted(by_id[t][1] for t in dict.fromkeys(target_ids)),
        })

    if changed:
        store.audit(
            conn, project_id=project_id, event_type="capability_needs_recorded",
            payload={"slugs": [r["slug"] for r in results if r["valid"]]},
        )
    conn.commit()

    source = store.latest_source(conn, project_id)
    scope = store.load_scope(conn, project_id)
    _, req_hash = store.scope_hash(scope, source)
    return {
        "project_id": project_id,
        "needs": results,
        "requirements_hash": req_hash,
        "blockers": store.scope_blockers(scope, source),
    }


# ---------------------------------------------------------------------------
# 4. retire_requirement
# ---------------------------------------------------------------------------

@_tool
def retire_requirement(
    conn: psycopg.Connection, *, req_id: str, reason: str,
) -> dict[str, Any] | None:
    """
    Take a requirement or a capability need out of scope, on the record.
    The row is kept; it stops counting everywhere. Re-recording the same
    slug brings it back.
    """
    req_id = _canon(req_id)
    if not req_id:
        return None
    bad = _bad_input(reason)
    if bad:
        return bad
    if not isinstance(reason, str) or is_blank(reason):
        return {"error": "reason is required"}
    if len(reason) > req_rules.MAX_FIELD_CHARS:
        return {"error": f"reason is longer than {req_rules.MAX_FIELD_CHARS} characters"}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT project_id, slug, kind, status FROM project_requirement WHERE id = %s",
            (req_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        project_id, slug, kind, status = str(row[0]), row[1], row[2], row[3]
        if status == "retired":
            return {"req_id": req_id, "slug": slug, "status": "retired", "changed": False}
        cur.execute(
            "UPDATE project_requirement SET status = 'retired', retired_reason = %s "
            "WHERE id = %s",
            (reason.strip(), req_id),
        )
    _demote_lapsed_tasks(conn, project_id)
    store.audit(
        conn, project_id=project_id, event_type="requirement_retired",
        subject_table="project_requirement", subject_id=req_id,
        payload={"slug": slug, "kind": kind, "reason": reason.strip()},
    )
    conn.commit()
    source = store.latest_source(conn, project_id)
    scope = store.load_scope(conn, project_id)
    return {
        "req_id": req_id, "slug": slug, "kind": kind, "status": "retired",
        "changed": True,
        "blockers": store.scope_blockers(scope, source),
        "note": "The requirements changed, so they must be confirmed again; an "
                "existing lock is stale until relocked.",
    }


# ---------------------------------------------------------------------------
# 5. request_requirements_confirmation  (human gate 1)
# ---------------------------------------------------------------------------

@_tool
def request_requirements_confirmation(
    conn: psycopg.Connection, *, project_id: str,
) -> dict[str, Any] | None:
    """
    Put the requirement set in front of the human.

    Opens an approval request for the current scope if it is complete,
    and reports its state. Search and decision stay closed until a human
    approves this exact content; any later edit re-closes them, and
    returning to content that was approved earlier needs approving again.
    """
    project = store.get_project(conn, project_id)
    if not project:
        return None
    project_id = project["id"]
    source = store.latest_source(conn, project_id)
    scope = store.load_scope(conn, project_id)
    blockers = store.scope_blockers(scope, source)
    snapshot, req_hash = store.scope_hash(scope, source)
    cov = store.scope_span_coverage(scope, source)

    if blockers:
        return {
            "status": "blocked",
            "blockers": blockers,
            "requirements_hash": req_hash,
            "span_coverage": _coverage_brief(cov),
        }

    state = store.confirmation(conn, project_id, req_hash)
    # What the human last said yes to, so the summary can show what moved.
    previous = pdr_approvals.latest_approved(
        conn, project_id=project_id, kind="requirements_confirmation",
    )
    previous_snapshot = ((previous or {}).get("subject") or {}).get("snapshot")
    if previous and (previous["subject"] or {}).get("requirements_hash") == req_hash:
        previous_snapshot = None                  # this IS the confirmed scope
    changes = req_rules.diff_snapshots(previous_snapshot, snapshot)
    summary = _render_requirements_summary(
        project["name"], scope, cov, source,
        changes=changes if previous_snapshot else None,
        retired=store.load_retired(conn, project_id),
        written_by_tool=store.file_written_by_tool(
            conn, (source or {}).get("source_file_hash")),
    )
    if state["state"] == "none":
        req = pdr_approvals.ensure_request(
            conn, project_id=project_id, kind="requirements_confirmation",
            subject=pdr_approvals.requirements_subject(
                requirements_hash=req_hash, snapshot=snapshot,
                follows_approval_id=state["follows_approval_id"],
            ),
            summary=summary,
        )
        if req["created"]:
            store.audit(
                conn, project_id=project_id,
                event_type="requirements_confirmation_requested",
                subject_table="approval_request", subject_id=req["id"],
                payload={"requirements_hash": req_hash},
            )
        conn.commit()
        state = store.confirmation(conn, project_id, req_hash)

    out: dict[str, Any] = {
        "status": {"pending": "awaiting_approval"}.get(state["state"], state["state"]),
        "approval_request_id": state["request_id"],
        "requirements_hash": req_hash,
        "span_coverage": _coverage_brief(cov),
        "changes_since_last_confirmation": changes if previous_snapshot else [],
        "summary_markdown": summary,
    }
    if state["state"] == "pending":
        out["how_to_approve"] = pdr_approvals.approve_hint(state["request_id"])
    elif state["state"] == "rejected":
        out["rejected_by"] = state["approver"]
        out["rejection_note"] = state["note"]
    else:
        out["approved_by"] = state["approver"]
    return out


def _render_requirements_summary(
    project_name: str,
    scope: dict[str, Any],
    cov: dict[str, Any] | None,
    source: dict[str, Any] | None,
    changes: list[dict[str, str]] | None = None,
    retired: list[dict[str, Any]] | None = None,
    written_by_tool: str | None = None,
) -> str:
    """
    What the human reads before confirming the scope. Every value is
    passed through one_line(): it is printed in a terminal, and the text
    comes from the agent, so it must not be able to redraw the screen or
    forge a section. CIP's own words here are plain ASCII.

    `changes` is the difference from the scope the human last confirmed
    (None when there is no earlier confirmation). It goes first: on a
    second confirmation, what moved is the thing being decided.
    """
    t = one_line
    reqs, needs = scope["requirements"], scope["needs"]
    lines = [f"# Requirements confirmation - {t(project_name)}", ""]

    lines.append("## The document these requirements come from")
    if source:
        lines.append(f"- Title: {t(source['title'])}")
        if source.get("raw_ref") and source["raw_ref"] != "inline":
            lines.append(f"- File: {t(source['raw_ref'])}")
            if source.get("source_file_hash"):
                lines.append(f"- SHA-256 of that file when ingested: {source['source_file_hash']}")
            if written_by_tool:
                lines.append(
                    f"- NOTE: THIS FILE WAS WRITTEN BY A CIP TOOL ({t(written_by_tool)}) AT AN "
                    f"AGENT'S REQUEST. Its content is the agent's, not a document a "
                    f"person supplied."
                )
        elif source.get("raw_ref") == "inline":
            lines.append(
                "- Source: TEXT SUPPLIED INLINE BY THE AGENT, not read from a file. "
                "There is no file to check it against."
            )
        else:
            lines.append("- Source: not recorded (ingested before v1.1)")
        lines.append(f"- Stored text hash: {source['content_hash'][:16]}")
        if source.get("replaced_another") or source.get("supersedes_id"):
            lines.append(
                "- NOTE: this is not the only text that has been this project's PDR. "
                "It replaced another one. Check it is the document you mean."
            )
    lines.append("")

    if changes is not None:
        lines.append("## Changes since the scope you last confirmed")
        if changes:
            for c in changes:
                detail = f" - {t(c['detail'])}" if c.get("detail") else ""
                lines.append(f"- {t(c['kind']).replace('_', ' ')}: {t(c['subject'])}{detail}")
        else:
            lines.append(
                "- the content differs from what you confirmed in a way not itemised "
                "here; read everything below"
            )
        lines.append("")

    counts: dict[str, int] = {}
    for r in reqs:
        counts[r["disposition"]] = counts.get(r["disposition"], 0) + 1
    lines.append(
        f"{len(reqs)} requirement(s): "
        + ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        + f". {len(needs)} capability need(s) to search."
    )
    lines.append("")

    if cov is not None:
        lines.append("## PDR sections with no requirement mapped")
        if cov["unmapped_sections"]:
            lines.append(
                f"{len(cov['unmapped_sections'])} of {cov['section_count']} sections. "
                f"Confirm each is genuinely free of requirements:"
            )
            for s in cov["unmapped_sections"]:
                lines.append(f"- {t(s['heading'], 120)}")
        else:
            lines.append(
                f"- none: every one of the {cov['section_count']} section(s) found "
                f"is quoted by something"
            )
        if cov["out_of_scope_only_sections"]:
            lines.append("")
            lines.append("## PDR sections quoted only by deferred or not-applicable items")
            lines.append("Nothing in this build comes from these. Confirm that is right:")
            for s in cov["out_of_scope_only_sections"]:
                lines.append(f"- {t(s['heading'], 120)}")
        if cov["lightly_quoted_sections"]:
            lines.append("")
            lines.append("## Long PDR sections that are barely quoted")
            lines.append(
                "A section counts as read once one quote touches it. In these, the "
                "quotes cover little of the text. Check nothing else in them is a requirement:"
            )
            for s in cov["lightly_quoted_sections"]:
                lines.append(f"- {t(s['heading'], 120)} ({s['quoted_pct']}% of it is quoted)")
        lines.append("")

    labels = (
        ("via_needs", "Satisfied through capability needs"),
        ("bespoke", "Bespoke - built without a reuse search"),
        ("deferred", "Deferred - out of this build"),
        ("not_applicable", "Not applicable"),
    )
    for disp, label in labels:
        group = [r for r in reqs if r["disposition"] == disp]
        if not group:
            continue
        lines.append(f"## {label} ({len(group)})")
        for r in group:
            lines.append(f"- {t(r['slug'])} [{t(r['kind'])}, {t(r['priority'])}] {t(r['text'])}")
            if r["quote"]:
                lines.append(f"  - PDR: \"{t(r['quote'])}\"")
            for c in r["acceptance_criteria"]:
                lines.append(f"  - accept: {t(c)}")
            if r["need_slugs"]:
                lines.append(f"  - needs: {', '.join(t(n) for n in r['need_slugs'])}")
            if r["disposition_reason"]:
                lines.append(f"  - reason: {t(r['disposition_reason'])}")
        lines.append("")

    if needs:
        lines.append(f"## Capability needs ({len(needs)})")
        for n in needs:
            lines.append(f"- {t(n['slug'])}: {t(n['description'])}")
            lines.append(f"  - serves: {', '.join(t(x) for x in n['satisfies'])}")
            # Each term in quotes: one term that contains what looks like a
            # list must not read as the list.
            terms = ", ".join(f'"{t(x)}"' for x in n["search_terms"])
            lines.append(f"  - will be searched for ({len(n['search_terms'])} term(s)): {terms}")
            if n["problem_types"]:
                lines.append(f"  - problem types: {', '.join(t(x) for x in n['problem_types'])}")
            else:
                lines.append(
                    "  - problem types: none (banked evaluations cannot be matched to "
                    "this need by tag)"
                )
            if n["constraints"]:
                lines.append(
                    "  - constraints: "
                    + "; ".join(f"{t(c['kind'])} {t(json.dumps(c['detail'], sort_keys=True))}"
                                for c in n["constraints"])
                )
            else:
                lines.append("  - constraints: none (the decision will be LLM judgment)")
        lines.append("")

    if retired:
        lines.append(f"## Retired - taken out altogether ({len(retired)})")
        for r in retired:
            what = "need" if r["kind"] == req_rules.NEED_KIND else "requirement"
            lines.append(f"- {t(r['slug'])} [{what}] {t(r['text'])}")
            lines.append(f"  - reason: {t(r['reason']) or 'none given'}")
    return "\n".join(lines).rstrip("\n")


def _require_confirmed_scope(
    conn, project_id: str, tool: str,
) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any] | None, str]:
    """
    Returns (refusal | None, scope, source, requirements_hash).
    The refusal is what a gated tool should return when the scope is not
    confirmed for its current content.
    """
    source = store.latest_source(conn, project_id)
    scope = store.load_scope(conn, project_id)
    _, req_hash = store.scope_hash(scope, source)
    state = store.confirmation(conn, project_id, req_hash)
    if state["state"] == "approved":
        return None, scope, source, req_hash
    if state["state"] == "pending":
        reason = (
            "The requirements are awaiting human confirmation. "
            + pdr_approvals.approve_hint(state["request_id"])
        )
    elif state["state"] == "rejected":
        reason = (
            f"The human rejected this requirement set ({one_line(state['note'], 200)}). "
            f"Revise it and call request_requirements_confirmation."
        )
    else:
        reason = (
            "The requirements are not confirmed in their current form. Any edit "
            "after confirmation re-opens the gate. Call "
            "request_requirements_confirmation and wait for the human."
        )
    refusal = _gate_refusal(conn, project_id, tool, reason, gate="requirements_confirmation")
    return refusal, scope, source, req_hash


# ---------------------------------------------------------------------------
# 6. search_for_requirement
# ---------------------------------------------------------------------------

def _clean_filter(value: Any) -> str | None:
    """A filter value, or None. An empty string is no filter, not a filter on ''."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


@_tool
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
    """
    Search the registry and the evaluation store for one capability need.

    With no search_query and no filters this is the mandatory sweep: every
    one of the need's confirmed search_terms, unfiltered, taking up to
    SWEEP_LIMIT hits per term whatever `limit` says. A term that returns
    that many is recorded as truncated, and CIP then will not conclude
    that nothing fits. record_decision requires the sweep. A custom query
    or a filtered search adds evidence but never replaces it.

    Every call is logged in requirement_search, including one that finds
    nothing — that row is the proof a search ran.
    """
    req_id = _canon(req_id)
    if not req_id:
        return None
    bad = _bad_input(search_query, ecosystem, capability_kind, component_kind,
                     runtime, cost_tier)
    if bad:
        return bad
    with conn.cursor() as cur:
        cur.execute(
            "SELECT project_id, kind, status FROM project_requirement WHERE id = %s",
            (req_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    project_id, kind, status = str(row[0]), row[1], row[2]
    if kind != req_rules.NEED_KIND:
        return _gate_refusal(
            conn, project_id, "search_for_requirement",
            "Search runs on capability needs, not on PDR requirements: a requirement "
            "sentence has no interface, license or dependency to match. Record the "
            "capability it needs with record_capability_needs and search that.",
        )
    if status != "active":
        return _gate_refusal(conn, project_id, "search_for_requirement",
                             "This capability need is retired.")

    refusal, scope, _source, _hash = _require_confirmed_scope(
        conn, project_id, "search_for_requirement",
    )
    if refusal:
        return refusal
    need = next(n for n in scope["needs"] if n["id"] == req_id)

    # Normalise before anything uses them: an empty string must not reach
    # the query as "ecosystem = ''" while being logged as "no filter".
    filters = {
        k: v for k, v in (
            ("ecosystem", _clean_filter(ecosystem)),
            ("capability_kind", _clean_filter(capability_kind)),
            ("component_kind", _clean_filter(component_kind)),
            ("runtime", _clean_filter(runtime)),
            ("cost_tier", _clean_filter(cost_tier)),
        ) if v
    }
    custom = search_query.strip() if isinstance(search_query, str) else ""
    queries = [custom] if custom else list(need["search_terms"])
    query_source = "custom" if custom else "need_terms"
    if not queries:
        return _gate_refusal(
            conn, project_id, "search_for_requirement",
            "This need has no search_terms. Re-record it with record_capability_needs.",
        )
    is_sweep = query_source == "need_terms" and not filters
    # The sweep takes everything up to its ceiling, whatever limit was
    # passed. `limit` only shapes extra, non-sweep searches.
    try:
        limit = int(limit)
    except (TypeError, ValueError, OverflowError):
        limit = 20
    effective_limit = SWEEP_LIMIT if is_sweep else max(1, min(limit, SWEEP_LIMIT))

    found: dict[str, dict[str, Any]] = {}
    truncated = False
    for q in queries:
        hits = search_capabilities(
            conn, q,
            ecosystem=filters.get("ecosystem"),
            capability_kind=filters.get("capability_kind"),
            component_kind=filters.get("component_kind"),
            runtime=filters.get("runtime"),
            cost_tier=filters.get("cost_tier"),
            # No project policy filter here: what it removes is counted
            # below, so a filter set elsewhere cannot make this search
            # look empty.
            project_id=None, limit=effective_limit,
        )
        if len(hits) >= effective_limit:
            truncated = True
        for hit in hits:
            found.setdefault(hit["id"], hit)
    allowed_ids = {
        h["id"] for h in _filter_by_constraints(conn, list(found.values()), project_id)
    }

    requirement = Requirement(
        id=req_id, slug=need["slug"], description=need["description"],
        constraints=tuple(Constraint(kind=c["kind"], detail=c["detail"])
                          for c in need["constraints"]),
    )
    evaluated: list[str] = []
    unevaluable: list[dict[str, Any]] = []
    policy_excluded = 0
    for hit in found.values():
        if hit["id"] not in allowed_ids:
            policy_excluded += 1
            unevaluable.append({
                "capability_id": hit["id"],
                "normalized_key": hit["normalized_key"],
                "display_name": hit["display_name"],
                "reason": "excluded by this project's policy constraints",
            })
            continue
        cv_id = hit.get("head_version_id")
        inputs = _load_candidate_inputs(conn, cv_id) if cv_id else None
        if inputs is None:
            unevaluable.append({
                "capability_id": hit["id"],
                "normalized_key": hit["normalized_key"],
                "display_name": hit["display_name"],
                "reason": ("no current version" if not cv_id else
                           "no source binding, so it cannot be pinned to a revision"),
            })
            continue
        _persist_fit_evaluation(
            conn, project_requirement_id=req_id, candidate=inputs,
            fit_result=evaluate_fit(requirement, inputs),
        )
        evaluated.append(cv_id)

    eval_matches = _search_evaluation_store(conn, need["problem_types"], queries)
    banked_slugs = sorted(m["asset_slug"] for m in eval_matches if m["counts_as_banked_match"])

    registry_size = store.registry_size(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO requirement_search
                (project_requirement_id, query_source, queries, filters,
                 need_fingerprint, registry_size, found_count,
                 evaluated_capability_version_ids, unevaluable_count, evaluation_slugs,
                 truncated, policy_excluded_count, registry_stamp)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s::uuid[], %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (req_id, query_source, queries, json.dumps(filters), need["fingerprint"],
             registry_size, len(found), evaluated, len(unevaluable), banked_slugs,
             truncated, policy_excluded, store.registry_stamp(conn)),
        )
        search_id = str(cur.fetchone()[0])
    conn.commit()

    kinds = [c["kind"] for c in need["constraints"]]
    candidates = _read_fit_evaluations(conn, req_id, evaluated, kinds)
    outcome = decision_rules.coverage_outcome(
        evaluated_count=len(evaluated), evaluation_hit_count=len(banked_slugs),
        unevaluable_count=len(unevaluable),
    )
    out: dict[str, Any] = {
        "req_id": req_id,
        "slug": need["slug"],
        "description": need["description"],
        "project_id": project_id,
        "constraints": need["constraints"],
        "search_id": search_id,
        "search_query": custom or None,
        "queries": queries,
        "query_source": query_source,
        "filters": filters,
        "counts_as_mandatory_sweep": is_sweep,
        "registry_size": registry_size,
        "truncated": truncated,
        "policy_excluded_count": policy_excluded,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "unevaluable": unevaluable,
        "evaluation_matches": eval_matches,
        "coverage_outcome": outcome,
    }
    if candidates and all(c["evidence"] == "insufficient" for c in candidates):
        out["note"] = (
            "CIP has no analysed evidence for any candidate on the points this need's "
            "constraints depend on (see missing_evidence). Their fit scores and "
            "blocking gaps reflect missing evidence, not findings. CIP will not "
            "compute a verdict from them; a choice among them is recorded as LLM "
            "judgment."
        )
    elif outcome == "found_but_unevaluable":
        out["note"] = (
            f"The registry returned {len(unevaluable)} match(es) and CIP could evaluate "
            f"none of them (see `unevaluable` for why: no source to pin, or excluded by "
            f"this project's policy). That is not the same as having nothing. A BUILD "
            f"here is flagged for the human."
        )
    elif outcome == "no_registry_coverage":
        out["note"] = (
            f"The registry ({registry_size} capabilities) has nothing for this need. "
            f"That is a statement about the registry's coverage, not proof nothing "
            f"exists. A BUILD here is recorded as a cold-start build."
        )
        if eval_matches:
            # Text matches in the evaluation store do not count as a banked
            # match (only a shared problem-type tag does), but they are not
            # nothing either.
            out["note"] += (
                f" The evaluation store has {len(eval_matches)} evaluation(s) that match "
                f"on text only (see `evaluation_matches`); read them before deciding."
                + ("" if need["problem_types"] else
                   " This need has no problem_types, so no banked evaluation can match "
                   "it by tag.")
            )
    elif outcome == "banked_only":
        out["note"] = (
            "Nothing selectable in the registry, but the evaluation store holds a "
            "matching banked asset. Ingest it to make it selectable, or record BUILD "
            "and expect it to be flagged."
        )
    if truncated:
        out["truncation_note"] = (
            f"At least one term returned {effective_limit} matches, the most this search "
            f"takes. There may be more it did not look at, so CIP will not conclude "
            f"that nothing fits. Narrower search terms (which need confirming) would "
            f"let it see everything."
        )
    return out


def _search_evaluation_store(
    conn, problem_types: list[str], terms: list[str],
) -> list[dict[str, Any]]:
    """
    Recall banked evaluations for a need.

    Tag overlap on problem_types is the strong signal and the only one
    that counts toward a "banked match". Text matches are returned for
    the reader but are too noisy to flag a decision on.

    linked_capability_id is derived by URL: an evaluation whose repo has
    been ingested into the registry is selectable there.
    """
    patterns = [
        "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        for t in terms if t
    ]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.asset_slug, e.asset_type, e.url, e.verdict, e.code_quality,
                   e.banked, e.evidence_depth, e.summary, e.problem_types,
                   e.license_notes, e.evaluated_at::text,
                   (e.problem_types && %(tags)s) AS tag_match,
                   (SELECT c.id::text FROM capability c
                     WHERE e.url IS NOT NULL
                       AND lower(rtrim(regexp_replace(c.metadata->>'html_url', '\\.git$', ''), '/'))
                         = lower(rtrim(regexp_replace(e.url, '\\.git$', ''), '/'))
                     LIMIT 1) AS linked_capability_id
            FROM evaluation e
            WHERE (e.problem_types && %(tags)s)
               OR (cardinality(%(pats)s::text[]) > 0 AND (
                       e.asset_slug   ILIKE ANY(%(pats)s)
                    OR e.summary      ILIKE ANY(%(pats)s)
                    OR e.useful_parts ILIKE ANY(%(pats)s)
                    OR EXISTS (SELECT 1 FROM unnest(e.problem_types) t
                                WHERE t ILIKE ANY(%(pats)s))))
            ORDER BY tag_match DESC, e.banked DESC, e.updated_at DESC, e.asset_slug
            LIMIT 20
            """,
            {"tags": list(problem_types), "pats": patterns},
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        tag_match = bool(r[11])
        usable = r[3] != "Reject" or bool(r[5])
        out.append({
            "asset_slug": r[0],
            "asset_type": r[1],
            "url": r[2],
            "verdict": r[3],
            "code_quality": r[4],
            "banked": r[5],
            "evidence_depth": r[6] or "unrecorded",
            "summary": r[7],
            "problem_types": list(r[8] or []),
            "license_notes": r[9],
            "evaluated_at": r[10],
            "match": "problem_type" if tag_match else "text",
            "counts_as_banked_match": tag_match and usable,
            "linked_capability_id": r[12],
            "selectable": r[12] is not None,
        })
    return out


# ---------------------------------------------------------------------------
# 7. record_decision
# ---------------------------------------------------------------------------

@_tool
def record_decision(
    conn: psycopg.Connection,
    *,
    req_id: str,
    verdict: str | None = None,
    chosen_capability_version_id: str | None = None,
    rationale: str = "",
) -> dict[str, Any] | None:
    """
    Record the decision for one capability need.

    CIP computes the verdict first, through the Step 12 rules, over the
    candidates this need's searches evaluated. `verdict` is the LLM's
    PROPOSAL, and may be omitted to take CIP's.

      - proposal agrees, or is omitted      → decided_by cip_rules
      - proposal differs (fit informative)  → llm_override, needs rationale
      - CIP cannot compute a verdict        → llm_judgment, needs rationale

    CIP cannot compute a verdict when the need has no machine-checkable
    constraint, or when it holds no evidence for the best-ranked candidate
    on the points those constraints depend on.

    Overrides and LLM judgments are listed for the human at lock time.
    Nothing here authorises code; the lock does.
    """
    req_id = _canon(req_id)
    if not req_id:
        return None
    bad = _bad_input(verdict, chosen_capability_version_id, rationale)
    if bad:
        return bad
    if not isinstance(rationale, str):
        return {"error": "rationale must be a string"}
    if len(rationale) > MAX_RATIONALE_CHARS:
        return {"error": f"rationale is longer than {MAX_RATIONALE_CHARS} characters"}
    if verdict is not None and not isinstance(verdict, str):
        return {"error": "verdict must be a string"}
    if chosen_capability_version_id is not None and not isinstance(
            chosen_capability_version_id, str):
        return {"error": "chosen_capability_version_id must be a string"}
    proposed = verdict.strip().upper() if isinstance(verdict, str) and verdict.strip() else None

    with conn.cursor() as cur:
        cur.execute(
            "SELECT project_id, kind, status FROM project_requirement WHERE id = %s",
            (req_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    project_id, kind, status = str(row[0]), row[1], row[2]
    tool = "record_decision"
    if proposed is not None and proposed not in decision_rules.VERDICTS:
        return _gate_refusal(conn, project_id, tool, f"Invalid verdict: {one_line(verdict, 40)}")
    if chosen_capability_version_id:
        canonical = _canon(chosen_capability_version_id)
        if not canonical:
            return _gate_refusal(conn, project_id, tool,
                                 "chosen_capability_version_id is not a UUID.")
        chosen_capability_version_id = canonical
    if kind != req_rules.NEED_KIND:
        return _gate_refusal(
            conn, project_id, tool,
            "Decisions are recorded on capability needs. A PDR requirement is decided "
            "through the needs linked to it, or marked bespoke / deferred / not_applicable.",
        )
    if status != "active":
        return _gate_refusal(conn, project_id, tool, "This capability need is retired.")

    refusal, scope, _source, _hash = _require_confirmed_scope(conn, project_id, tool)
    if refusal:
        return refusal
    need = next(n for n in scope["needs"] if n["id"] == req_id)

    searches = [s for s in store.load_searches(conn, req_id)
                if s["need_fingerprint"] == need["fingerprint"]]
    if not any(store.counts_as_sweep(s, need["fingerprint"]) for s in searches):
        return _gate_refusal(
            conn, project_id, tool,
            "No search on record for this need as it is currently defined. Call "
            "search_for_requirement with no search_query and no filters first: that "
            "runs the need's own search_terms and is the proof a search happened. "
            "A custom or filtered search does not count, and neither does a search "
            "made before the need's terms or constraints changed.",
            gate="search_before_decision",
        )

    # The sweep the decision rests on is the latest one. It has to describe
    # the registry as it is: a sweep of an empty registry says nothing
    # about a candidate ingested since.
    sweep = [s for s in searches if store.counts_as_sweep(s, need["fingerprint"])][-1]
    registry_now = store.registry_size(conn)
    # Compared by fingerprint, not by count: ingesting or analysing a match
    # the sweep could not evaluate changes what the sweep would say and
    # adds no capability.
    if (sweep["registry_size"] != registry_now
            or sweep["registry_stamp"] != store.registry_stamp(conn)):
        return _gate_refusal(
            conn, project_id, tool,
            f"The registry has changed since this need was swept ({sweep['registry_size']} "
            f"capabilities then, {registry_now} now; something was added, ingested, "
            f"analysed or superseded, or an evaluation was recorded). Run the sweep again "
            f"(search_for_requirement with no query and no filters), then decide.",
            gate="search_before_decision",
        )

    evaluated = list(dict.fromkeys(
        cv for s in searches for cv in s["evaluated_capability_version_ids"]
    ))
    banked = sorted({slug for s in searches for slug in s["evaluation_slugs"]})
    kinds = [c["kind"] for c in need["constraints"]]

    # CIP computes, through the Step 12 orchestrator. Preview first: the
    # write replaces the recommendation row and cascades away any
    # build_authorization, which must not happen for a decision that ends
    # up refused below.
    from workers.recommend import (
        load_default_rules,
        persist_recommendation,
        preview_recommendation,
    )
    computed = preview_recommendation(
        conn,
        project_requirement_id=uuid.UUID(req_id),
        rules_profile=load_default_rules(),
        candidate_capability_version_ids=[uuid.UUID(c) for c in evaluated],
    )

    fits = _read_fit_evaluations(conn, req_id, evaluated, kinds)
    # Blocking gaps that rest on evidence, as opposed to ones that only
    # record that the evidence is missing.
    blocking = [f["capability_version_id"] for f in fits if f["confirmed_blocking_gaps"]]
    unanalysed = [f["capability_version_id"] for f in fits if f["evidence"] == "insufficient"]
    # Step 12 applies the rules to the best-ranked candidate only. These are
    # the candidates the same rules would give a reuse verdict, each taken
    # on its own: "the rules rejected it" must not be reported as "the
    # rules rejected them all".
    rule_accepted = [
        cv for cv, dec in computed.rule_decisions.items()
        if dec.verdict.value in decision_rules.REUSE_VERDICTS
    ]

    outcome = decision_rules.classify_decision(
        constraint_kinds=kinds,
        computed_verdict=computed.verdict.value,
        computed_capability_version_id=(
            str(computed.chosen_capability_version_id)
            if computed.chosen_capability_version_id else None
        ),
        proposed_verdict=proposed,
        proposed_capability_version_id=chosen_capability_version_id,
        evaluated_capability_version_ids=evaluated,
        blocking_gap_capability_version_ids=blocking,
        evaluation_hit_count=len(banked),
        rationale=rationale,
        winner_capability_version_id=(
            computed.winner.inputs.capability_version_id if computed.winner else None
        ),
        unanalysed_capability_version_ids=unanalysed,
        unevaluable_count=sweep["unevaluable_count"],
        sweep_truncated=sweep["truncated"],
        rule_accepted_capability_version_ids=rule_accepted,
    )
    if isinstance(outcome, decision_rules.DecisionRefusal):
        return _gate_refusal(conn, project_id, tool, outcome.reason)

    flags = list(outcome.flags)
    replaced_banked: list[str] = []
    if outcome.final_verdict == "BUILD":
        replaced_banked = store.replaced_banked_evaluations(
            conn, project_id, need["problem_types"],
        )
        if replaced_banked:
            flags.append(decision_rules.FLAG_BANKED_CHANGED)

    pin_rev = pin_asset = None
    if outcome.final_capability_version_id:
        inputs = _load_candidate_inputs(conn, outcome.final_capability_version_id)
        if inputs is None:
            return _gate_refusal(
                conn, project_id, tool,
                "The chosen capability version has no source binding, so it cannot be "
                "pinned to an exact revision.",
            )
        pin_rev, pin_asset = inputs.pinned_revision_key, inputs.pinned_source_asset_key

    # The decision stands. Write the recommendation + candidate rows Step 13
    # reads as its proof of search, and the decision record, in ONE
    # transaction: replacing the recommendation removes its build
    # authorisation, and that must not happen without the decision that
    # explains it.
    recommendation = persist_recommendation(conn, computed, commit=False)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO decision_record
                (project_requirement_id, recommendation_id, search_ids, need_fingerprint,
                 computed_verdict, computed_rule_name, computed_capability_version_id,
                 final_verdict, final_capability_version_id, pinned_revision_key,
                 pinned_source_asset_key, decided_by, coverage_outcome, flags,
                 evaluation_slugs, rationale)
            VALUES (%s, %s, %s::uuid[], %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                req_id, str(recommendation.recommendation_id),
                [s["id"] for s in searches], need["fingerprint"],
                outcome.computed_verdict,
                computed.rule_name if outcome.computed_verdict else None,
                outcome.computed_capability_version_id,
                outcome.final_verdict, outcome.final_capability_version_id,
                pin_rev, pin_asset, outcome.decided_by, outcome.coverage_outcome,
                flags, banked, rationale.strip(),
            ),
        )
        decision_id = str(cur.fetchone()[0])
    store.audit(
        conn, project_id=project_id, event_type="decision_recorded",
        subject_table="decision_record", subject_id=decision_id,
        payload={"need": need["slug"], "final_verdict": outcome.final_verdict,
                 "decided_by": outcome.decided_by, "flags": flags,
                 "replaced_banked_evaluations": replaced_banked},
    )
    conn.commit()

    return {
        "decision_record_id": decision_id,
        "recommendation_id": str(recommendation.recommendation_id),
        "need": need["slug"],
        "verdict": outcome.final_verdict,
        "chosen_capability_version_id": outcome.final_capability_version_id,
        "pinned_revision": pin_rev,
        "decided_by": outcome.decided_by,
        "computed_verdict": outcome.computed_verdict,
        "computed_rule": computed.rule_name if outcome.computed_verdict else None,
        "coverage_outcome": outcome.coverage_outcome,
        "flags": flags,
        "listed_for_human": outcome.decided_by != "cip_rules" or bool(flags),
        "note": outcome.note or None,
    }


# ---------------------------------------------------------------------------
# 8/9. build_approval_brief + lock_architecture  (human gate 2)
# ---------------------------------------------------------------------------

def _prepare_lock(
    conn: psycopg.Connection,
    *,
    project: dict[str, Any],
    architecture_doc_path: str | None = None,
    architecture_doc_text: str | None = None,
    approved_providers: Any = None,
    cost_commitments: Any = None,
    project_decisions: Any = None,
    unresolved_risks: Any = None,
    replay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Everything a lock needs, computed once and used by both the brief
    (a view) and lock_architecture (the write), so the two cannot
    disagree about what is being approved.

    `replay` is the subject of an already-approved request. Its inputs are
    used as they were approved instead of being supplied again, so a
    session that did not open the request can still complete it.
    """
    project_id = project["id"]
    blockers: list[str] = []

    source = store.latest_source(conn, project_id)
    scope = store.load_scope(conn, project_id)
    _, req_hash = store.scope_hash(scope, source)
    blockers += store.scope_blockers(scope, source)
    state = store.confirmation(conn, project_id, req_hash)
    if state["state"] != "approved" and not blockers:
        blockers.append(
            "the requirements are not confirmed in their current form "
            "(request_requirements_confirmation, then the human approves)"
        )

    doc_hash = doc_ref = doc_file_hash = None
    if replay is not None:
        providers = replay.get("approved_providers") or []
        costs = replay.get("cost_commitments") or []
        proj_decisions = replay.get("project_decisions") or []
        risks = replay.get("unresolved_risks") or []
        doc_hash = replay.get("architecture_doc_hash")
        doc_ref = replay.get("architecture_doc_ref")
        doc_file_hash = replay.get("architecture_doc_file_hash")
    else:
        providers, e1 = lock_rules.validate_providers(approved_providers)
        costs, e2 = lock_rules.validate_cost_commitments(cost_commitments)
        proj_decisions, e3 = lock_rules.validate_project_decisions(project_decisions)
        risks, e4 = lock_rules.validate_risks(unresolved_risks)
        blockers += e1 + e2 + e3 + e4

        if architecture_doc_path and architecture_doc_text:
            blockers.append("give architecture_doc_path or architecture_doc_text, not both")
        elif architecture_doc_path:
            text, doc_file_hash, err = _read_document(
                architecture_doc_path, MAX_DOC_BYTES, "architecture document",
            )
            if err:
                blockers.append(err)
                doc_file_hash = None
            else:
                doc_hash = text_hash(normalise_text(text))
                doc_ref = str(Path(architecture_doc_path.strip()))
        elif architecture_doc_text:
            if not isinstance(architecture_doc_text, str):
                blockers.append("architecture_doc_text must be a string")
            else:
                doc_hash, doc_ref = text_hash(normalise_text(architecture_doc_text)), "inline"

    current = store.current_decisions(conn, project_id)
    # Facts for the revision each decision pinned, not for whatever is
    # newest now: the summary prints the pin and the license side by side.
    # Keyed by decision: two needs can pin different revisions of one
    # capability, and each line must describe its own.
    facts = store.decision_facts(conn, list(current.values()))
    computed_keys = store.capability_facts(
        conn, [d["computed_capability_version_id"] for d in current.values()
               if d["computed_capability_version_id"]],
    )
    decisions: list[dict[str, Any]] = []
    for need in scope["needs"]:
        d = current.get(need["id"])
        if d is None:
            blockers.append(f"capability need '{need['slug']}' has no decision")
            continue
        if d["need_fingerprint"] != need["fingerprint"]:
            blockers.append(
                f"capability need '{need['slug']}' changed after it was decided; "
                f"search and decide it again"
            )
            continue
        if _recommendation_id(conn, need["id"]) is None:
            blockers.append(
                f"capability need '{need['slug']}' has no recommendation row; "
                f"run record_decision for it again"
            )
            continue
        cap = facts.get(d["id"], {})
        decisions.append({
            "need_id": need["id"],
            "need_slug": need["slug"],
            "decision_record_id": d["id"],
            "verdict": d["final_verdict"],
            "capability_version_id": d["final_capability_version_id"],
            "capability_key": cap.get("capability_key"),
            "pinned_revision": d["pinned_revision_key"],
            "pinned_source_asset": d["pinned_source_asset_key"],
            "decided_by": d["decided_by"],
            "coverage_outcome": d["coverage_outcome"],
            "flags": d["flags"],
            "license": cap.get("license"),
            "license_source": cap.get("license_source"),
            "cost_tier": cap.get("cost_tier"),
            "satisfies": need["satisfies"],
            "search_terms": sorted(need["search_terms"]),
            # What CIP computed (null when it could not stand behind a
            # verdict) and the agent's reason for anything else. An
            # override reaches the human with both sides of it.
            "computed_verdict": d["computed_verdict"],
            "computed_capability_key": (computed_keys.get(
                d["computed_capability_version_id"] or "", {}).get("capability_key")),
            "rationale": d["rationale"] or "",
        })

    by_disp: dict[str, list[str]] = {
        "in_scope": [], "bespoke": [], "deferred": [], "not_applicable": [],
    }
    for r in scope["requirements"]:
        disp = r["disposition"]
        if disp in req_rules.IN_SCOPE_DISPOSITIONS:
            by_disp["in_scope"].append(r["slug"])
        if disp in ("bespoke", "deferred", "not_applicable"):
            by_disp[disp].append(r["slug"])

    active = store.load_active_lock(conn, project_id)

    def build(follows: str | None) -> dict[str, Any]:
        return lock_rules.build_lock_payload(
            project_id=project_id,
            requirements_hash=req_hash,
            architecture_doc_hash=doc_hash,
            decisions=decisions,
            project_decisions=proj_decisions,
            approved_providers=providers,
            cost_commitments=costs,
            unresolved_risks=risks,
            scope=by_disp,
            replaces_lock_version=active["version"] if active else None,
            architecture_doc_ref=doc_ref,
            architecture_doc_file_hash=doc_file_hash,
            follows_lock_request=follows,
            # Always the confirmation in force NOW, on a replay too: an
            # approval given under an earlier confirmation of the same
            # content then no longer matches.
            requirements_approval_id=(state["approval_id"]
                                      if state["state"] == "approved" else None),
        )

    # Where this payload sits in the history of lock requests. If the
    # latest request already asks for this content, this IS that request
    # (asking twice opens nothing new). Otherwise it follows the latest
    # one, which makes it new content to approve even if the same content
    # was approved at some earlier point.
    latest_request_id = pdr_approvals.latest_requested_lock_request(conn, project_id)
    if replay is not None:
        payload = build(replay.get("follows_lock_request"))
    else:
        payload = build(latest_request_id)
        latest = (pdr_approvals.get_request(conn, latest_request_id)
                  if latest_request_id else None)
        if latest and isinstance(latest.get("subject"), dict):
            same_point = build(latest["subject"].get("follows_lock_request"))
            if lock_rules.payload_hash(same_point) == latest["subject_hash"]:
                payload = same_point
    old_payload = store.lock_payload(active) if active else None
    if active and old_payload is None:
        diff = {"is_first_lock": False, "changes": [{
            "kind": "legacy_lock_replaced", "subject": f"lock v{active['version']}",
            "detail": "created before v1.1 without a recorded approval; this lock replaces it",
        }]}
    else:
        diff = lock_rules.diff_payloads(old_payload, payload)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM architecture_lock WHERE project_id = %s",
            (project_id,),
        )
        next_version = cur.fetchone()[0]

    flags = lock_rules.lock_flags(payload)
    return {
        "blockers": blockers,
        "payload": payload,
        "payload_hash": lock_rules.payload_hash(payload),
        "flags": flags,
        "diff": diff,
        "next_version": next_version,
        "architecture_doc_ref": doc_ref,
        "latest_request_id": latest_request_id,
        "active_lock": active,
        "old_payload": old_payload,
        "scope": scope,
        "summary": lock_rules.render_lock_summary(
            project_name=project["name"], next_version=next_version,
            payload=payload, flags=flags, diff=diff,
        ),
    }


def build_approval_brief(
    conn: psycopg.Connection,
    *,
    project_id: str,
    architecture_doc_path: str | None = None,
    architecture_doc_text: str | None = None,
    approved_providers: list[dict[str, Any]] | None = None,
    cost_commitments: list[dict[str, Any]] | None = None,
    project_decisions: list[dict[str, Any]] | None = None,
    unresolved_risks: list[str] | None = None,
) -> dict[str, Any] | None:
    """
    Preview exactly what lock_architecture would freeze. Writes nothing.

    Pass the same inputs you will pass to lock_architecture; the brief
    and the lock are computed by one function.
    """
    project = store.get_project(conn, project_id)
    if not project:
        return None
    project_id = project["id"]
    bad = _bad_input(architecture_doc_path, architecture_doc_text, approved_providers,
                     cost_commitments, project_decisions, unresolved_risks)
    if bad:
        return bad
    prep = _prepare_lock(
        conn, project=project,
        architecture_doc_path=architecture_doc_path,
        architecture_doc_text=architecture_doc_text,
        approved_providers=approved_providers, cost_commitments=cost_commitments,
        project_decisions=project_decisions, unresolved_risks=unresolved_risks,
    )
    state = pdr_approvals.request_state(
        conn, project_id=project_id, kind="architecture_lock",
        subject_hash=prep["payload_hash"],
    )
    return {
        "brief_markdown": prep["summary"],
        "decisions": prep["payload"]["decisions"],
        "human_approval_items": prep["flags"],
        "unresolved": prep["blockers"],
        "changes_from_active_lock": prep["diff"]["changes"],
        "payload_hash": prep["payload_hash"],
        "approval_state": state["state"],
        "ready_to_lock": not prep["blockers"],
    }


@_tool
def lock_architecture(
    conn: psycopg.Connection,
    *,
    project_id: str,
    architecture_doc_path: str | None = None,
    architecture_doc_text: str | None = None,
    approved_providers: list[dict[str, Any]] | None = None,
    cost_commitments: list[dict[str, Any]] | None = None,
    project_decisions: list[dict[str, Any]] | None = None,
    unresolved_risks: list[str] | None = None,
    approval_request_id: str | None = None,
    approved_by: str | None = None,
) -> dict[str, Any] | None:
    """
    Freeze the approved decisions into a new lock version.

    Two calls, with a human in between:
      1st call — with the lock inputs. Computes the payload and opens an
                 approval request. Returns status 'awaiting_approval'.
      (human)  — reads and approves it with `cip-composer approvals`.
      2nd call — with approval_request_id. Replays exactly what was
                 approved and writes the lock. (Passing the same inputs
                 again also works.)

    `approved_by` is ignored. In v1.0 it was free text supplied by the
    caller, which let the governed party sign its own approval. The
    approver, and any approval note, come from the approval record.

    An approval is tied to the lock version it replaces: it cannot be
    used once a newer lock exists, and it is used at most once.

    Writing the lock also applies each final verdict to the
    recommendation row and creates the Step 13 build_authorization for
    every BUILD need, in the human approver's name.
    """
    project = store.get_project(conn, project_id)
    if not project:
        return None
    project_id = project["id"]
    bad = _bad_input(architecture_doc_path, architecture_doc_text, approved_providers,
                     cost_commitments, project_decisions, unresolved_risks,
                     approval_request_id, approved_by)
    if bad:
        return bad
    tool = "lock_architecture"

    replay = None
    if approval_request_id:
        if any(x for x in (architecture_doc_path, architecture_doc_text, approved_providers,
                           cost_commitments, project_decisions, unresolved_risks)):
            return {"error": "pass approval_request_id on its own: it replays the inputs "
                             "that were approved"}
        req_row = pdr_approvals.get_request(conn, approval_request_id)
        if (req_row is None or req_row["project_id"] != project_id
                or req_row["kind"] != "architecture_lock"):
            return {"error": "approval_request_id is not a lock request for this project"}
        replay = req_row["subject"] or {}

    # One lock call per project at a time. Each call reads "the latest
    # request" and then writes relative to it; two that overlap would both
    # follow the same one. Held until this transaction ends, and every path
    # below ends it (a commit, or the rollback in a recorded refusal).
    _one_at_a_time(conn, project_id)
    prep = _prepare_lock(
        conn, project=project,
        architecture_doc_path=architecture_doc_path,
        architecture_doc_text=architecture_doc_text,
        approved_providers=approved_providers, cost_commitments=cost_commitments,
        project_decisions=project_decisions, unresolved_risks=unresolved_risks,
        replay=replay,
    )
    if prep["blockers"]:
        return _gate_refusal(
            conn, project_id, tool, "Cannot lock: " + "; ".join(prep["blockers"]),
            blockers=prep["blockers"],
        )

    payload, p_hash = prep["payload"], prep["payload_hash"]
    active = prep["active_lock"]
    if (active and prep["old_payload"] is not None
            and lock_rules.content_core_hash(prep["old_payload"])
            == lock_rules.content_core_hash(payload)
            and not store.lock_stale_reasons(
                active, payload["requirements_hash"],
                store.confirmation(conn, project_id, payload["requirements_hash"])["state"])):
        # Same content as the active lock: nothing to approve. Re-assert the
        # lock's effects, in case a recommendation row was rebuilt outside
        # this workflow and lost its build authorisation.
        refusal, authorised = _apply_lock_effects(
            conn, payload=prep["old_payload"], approver=active["approved_by"],
            approval_id=active["approval_id"], lock_id=active["id"],
            version=active["version"],
        )
        if refusal:
            return refusal
        conn.commit()
        return {
            "architecture_lock_id": active["id"],
            "version": active["version"],
            "payload_hash": active["payload_hash"],
            "source_lock_hash": active["source_lock_hash"],
            "decision_count": len(active["decisions"]),
            "approved_by": active["approved_by"],
            "build_authorizations": authorised,
            "unchanged": True,
        }

    if replay is not None and req_row["id"] != prep["latest_request_id"]:
        # Approve A; the human changes their mind; B is requested. A's
        # approval is still on record, and it is no longer the human's
        # latest word. Only the most recent request can become the lock.
        return _gate_refusal(
            conn, project_id, tool,
            "A later lock request has been made since that one, so it can no longer "
            "be written: only the most recent lock request can become the lock. Use "
            "the latest request (pdr_status shows it), or call lock_architecture with "
            "the lock inputs you want, which opens a new request for the human.",
            approval_request_id=req_row["id"],
        )
    if replay is not None and p_hash != req_row["subject_hash"]:
        return _gate_refusal(
            conn, project_id, tool,
            "That approval no longer matches the project: a decision, the requirements "
            "or the active lock changed after it was requested, or the requirements "
            "were confirmed again since. Call lock_architecture with the lock inputs "
            "to open a fresh request.",
            approval_request_id=req_row["id"],
        )

    req = pdr_approvals.ensure_request(
        conn, project_id=project_id, kind="architecture_lock",
        subject=payload, summary=prep["summary"],
    )
    # The lock request asked for most recently is the live one (see
    # pdr_approvals.obsolete_reason). A request that already existed is the
    # latest one by construction: _prepare_lock only reuses the latest
    # request's place in history, so anything else is a new request. If
    # that ever fails to hold, asking again makes it the latest, so that a
    # request the tool just returned is never one nobody can approve.
    if req["created"] or req["id"] != pdr_approvals.latest_requested_lock_request(
            conn, project_id):
        store.audit(
            conn, project_id=project_id, event_type="lock_approval_requested",
            subject_table="approval_request", subject_id=req["id"],
            payload={"payload_hash": p_hash, "version": prep["next_version"],
                     "architecture_doc_ref": prep["architecture_doc_ref"],
                     "asked_again": not req["created"],
                     "flags": [f["code"] for f in prep["flags"]]},
        )
    conn.commit()

    state = pdr_approvals.request_state(
        conn, project_id=project_id, kind="architecture_lock", subject_hash=p_hash,
    )
    warnings = []
    if approved_by:
        warnings.append(
            "approved_by is ignored: the approver is whoever approves the request "
            "in a terminal."
        )
    if state["state"] == "pending":
        return {
            "status": "awaiting_approval",
            "approval_request_id": req["id"],
            "payload_hash": p_hash,
            "version": prep["next_version"],
            "human_approval_items": prep["flags"],
            "changes_from_active_lock": prep["diff"]["changes"],
            "summary_markdown": prep["summary"],
            "how_to_approve": pdr_approvals.approve_hint(req["id"]),
            "then": f"lock_architecture(project_id, approval_request_id='{req['id']}')",
            "warnings": warnings,
        }
    if state["state"] == "rejected":
        return _gate_refusal(
            conn, project_id, tool,
            f"The human rejected this lock request ({one_line(state['note'], 300)}). "
            f"Change what was objected to; a different payload opens a new request.",
            approval_request_id=req["id"], rejected_by=state["approver"],
        )

    # Approved for this exact payload. The commit above let go of the
    # project; take it again for the write, and look once more: a request
    # made in the gap is now the human's latest word.
    _one_at_a_time(conn, project_id)
    if pdr_approvals.latest_requested_lock_request(conn, project_id) != req["id"]:
        return _gate_refusal(
            conn, project_id, tool,
            "A later lock request was made while this one was being written, so it "
            "can no longer become the lock. Call pdr_status.",
            approval_request_id=req["id"],
        )
    # An approval writes one lock, ever.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version FROM architecture_lock WHERE approval_id = %s",
            (state["approval_id"],),
        )
        used = cur.fetchone()
    if used:
        return _gate_refusal(
            conn, project_id, tool,
            f"That approval was already used to write lock v{used[0]}. An approval is "
            f"for one lock; request a new one.",
        )

    approver = state["approver"]
    version = prep["next_version"]
    with conn.cursor() as cur:
        rejected_alternatives = _rejected_alternatives(conn, payload["decisions"])
        cur.execute(
            "UPDATE architecture_lock SET status = 'superseded' "
            "WHERE project_id = %s AND status = 'active'",
            (project_id,),
        )
        cur.execute(
            """
            INSERT INTO architecture_lock
                (project_id, version, status, approved_by, approval_note,
                 decisions, rejected_alternatives, approved_providers,
                 cost_commitments, unresolved_risks, source_lock_hash,
                 approval_id, payload_hash, requirements_hash,
                 architecture_doc_hash, architecture_doc_ref,
                 project_decisions, scope, replaces_version,
                 architecture_doc_file_hash, follows_request_id,
                 requirements_approval_id)
            VALUES (%s, %s, 'active', %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s,
                    %s, %s, %s)
            RETURNING id
            """,
            (
                project_id, version, approver, state["note"],
                json.dumps(payload["decisions"]), json.dumps(rejected_alternatives),
                json.dumps(payload["approved_providers"]),
                json.dumps(payload["cost_commitments"]),
                json.dumps(payload["unresolved_risks"]),
                lock_rules.decisions_hash(payload),
                state["approval_id"], p_hash, payload["requirements_hash"],
                payload["architecture_doc_hash"], payload["architecture_doc_ref"],
                json.dumps(payload["project_decisions"]), json.dumps(payload["scope"]),
                payload["replaces_lock_version"],
                payload["architecture_doc_file_hash"],
                payload["follows_lock_request"],
                payload["requirements_approval_id"],
            ),
        )
        lock_id = str(cur.fetchone()[0])

    refusal, authorised = _apply_lock_effects(
        conn, payload=payload, approver=approver,
        approval_id=state["approval_id"], lock_id=lock_id, version=version,
    )
    if refusal:
        return refusal
    _demote_lapsed_tasks(conn, project_id)
    abandoned = _abandon_stale_tasks(conn, project_id, version)

    store.audit(
        conn, project_id=project_id, event_type="architecture_locked",
        subject_table="architecture_lock", subject_id=lock_id, actor=approver,
        payload={"version": version, "payload_hash": p_hash,
                 "approval_id": state["approval_id"],
                 "build_authorizations": authorised},
    )
    conn.commit()
    return {
        "architecture_lock_id": lock_id,
        "version": version,
        "payload_hash": p_hash,
        "source_lock_hash": lock_rules.decisions_hash(payload),
        "decision_count": len(payload["decisions"]),
        "approved_by": approver,
        "build_authorizations": authorised,
        # Tasks this lock does not carry (their requirement left the build
        # or their decision changed). They are abandoned; create new ones.
        "tasks_abandoned": abandoned,
        "warnings": warnings,
    }


def _apply_lock_effects(
    conn, *, payload: dict[str, Any], approver: str, approval_id: str,
    lock_id: str, version: int,
) -> tuple[dict[str, Any] | None, int]:
    """
    Make the rest of the system agree with an approved lock:
      - every recommendation row says what was approved
      - every BUILD need has a Step 13 build_authorization, in the name of
        the human who approved the lock

    Idempotent. Returns (refusal | None, number of BUILD needs authorised).
    Rolls back and returns a refusal if Step 13 will not authorise.
    """
    authorised = 0
    with conn.cursor() as cur:
        for d in payload["decisions"]:
            _apply_final_verdict(cur, d)
    for d in payload["decisions"]:
        if d["verdict"] != "BUILD":
            continue
        rec_id = _recommendation_id(conn, d["need_id"])
        # Replace any authorisation that did not come from this approval, so
        # the row always names the human who approved the lock.
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM build_authorization WHERE recommendation_id = %s "
                "AND (metadata->>'approval_id') IS DISTINCT FROM %s",
                (rec_id, approval_id),
            )
        try:
            authorize_build(
                conn,
                recommendation_id=uuid.UUID(rec_id),
                authorized_by=approver,
                reason=f"Approved in architecture lock v{version} (approval {approval_id})",
                metadata={"architecture_lock_id": lock_id,
                          "approval_id": approval_id,
                          "decision_record_id": d["decision_record_id"]},
            )
            authorised += 1
        except BuildGateError as e:
            return _gate_refusal(
                conn, payload["project_id"], "lock_architecture",
                f"Step 13 refused to authorise BUILD for '{d['need_slug']}': {e}. "
                f"Nothing was locked.",
            ), 0
    return None, authorised


def _recommendation_id(conn, need_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM recommendation WHERE project_requirement_id = %s", (need_id,),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


def _apply_final_verdict(cur, d: dict[str, Any]) -> None:
    """
    Make the recommendation row say what was approved.

    recommendation is the single "current verdict" row Step 13 reads.
    Until the lock it holds what CIP's rules computed; an approved
    override or LLM judgment becomes effective here, under the human
    approval that covers the whole lock.
    """
    cur.execute(
        "SELECT id, verdict, rule_name FROM recommendation WHERE project_requirement_id = %s",
        (d["need_id"],),
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError(
            f"need '{d['need_slug']}' has no recommendation row; run record_decision again"
        )
    rec_id, verdict, rule_name = row
    # '__no_candidates__' is what Step 13 reads as proof that a search ran
    # and evaluated nothing. It stays true whoever made the decision: an
    # LLM-judged BUILD over a truncated sweep that evaluated nothing was
    # otherwise approved by the human and then refused by Step 13, for good.
    if rule_name == "__no_candidates__" and d["verdict"] == "BUILD":
        pass
    elif d["decided_by"] == "llm_override":
        rule_name = "__llm_override__"
    elif d["decided_by"] == "llm_judgment":
        rule_name = "__llm_judgment__"
    cur.execute(
        """
        UPDATE recommendation
        SET verdict = %s, chosen_capability_version_id = %s,
            pinned_revision_key = %s, pinned_source_asset_key = %s, rule_name = %s
        WHERE id = %s
        """,
        (d["verdict"], d["capability_version_id"], d["pinned_revision"],
         d["pinned_source_asset"], rule_name, rec_id),
    )
    if d["verdict"] != "BUILD":
        cur.execute("DELETE FROM build_authorization WHERE recommendation_id = %s", (rec_id,))


def _rejected_alternatives(conn, decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Candidates that were evaluated for a need and not chosen."""
    out: list[dict[str, Any]] = []
    for d in decisions:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT search_ids FROM decision_record WHERE id = %s",
                (d["decision_record_id"],),
            )
            search_ids = [str(x) for x in (cur.fetchone() or [[]])[0]]
            cur.execute(
                "SELECT evaluated_capability_version_ids FROM requirement_search "
                "WHERE id = ANY(%s::uuid[])",
                (search_ids,),
            )
            cv_ids = sorted({str(c) for r in cur.fetchall() for c in (r[0] or [])})
        for f in _read_fit_evaluations(conn, d["need_id"], cv_ids, []):
            if f["capability_version_id"] == d["capability_version_id"]:
                continue
            out.append({
                "need_slug": d["need_slug"],
                "capability_key": f["normalized_key"],
                "capability_version_id": f["capability_version_id"],
                "fit_score": f"{f['fit_score']:.3f}",
                "blocking_gap_count": f["blocking_gap_count"],
            })
    return out


# ---------------------------------------------------------------------------
# 10. check_against_lock
# ---------------------------------------------------------------------------

@_tool
def check_against_lock(
    conn: psycopg.Connection,
    *,
    project_id: str,
    proposed_change: dict[str, Any],
) -> dict[str, Any] | None:
    """
    Test a proposed change against the active lock.

    Deterministic, structured, fail-closed. Every call is logged, so
    there is evidence afterwards of whether the agent checked.
    """
    project = store.get_project(conn, project_id)
    if not project:
        return None
    project_id = project["id"]

    facts = _facts(conn, project_id)
    lock = facts["lock"]

    unreadable = first_bad(proposed_change)
    if unreadable:
        store.audit(
            conn, project_id=project_id, event_type="lock_check",
            subject_table="architecture_lock", subject_id=lock["id"] if lock else None,
            payload={"verdict": lock_check_rules.BLOCKED, "reasons": [unreadable],
                     "lock_version": lock["version"] if lock else None},
        )
        conn.commit()
        return {
            "verdict": lock_check_rules.BLOCKED,
            "reason": f"The change could not be read: {unreadable}.",
            "reasons": [f"The change could not be read: {unreadable}."],
            "cited": [], "reminders": [],
            "lock_version": lock["version"] if lock else None,
            "next": "Resolve what is blocking and check again.",
        }

    lock_view = None
    if lock:
        lock_view = lock_check_rules.LockView(
            version=lock["version"],
            stale_reasons=tuple(facts["stale_reasons"]),
            decisions=facts["locked_decisions"],
            approved_providers=tuple(lock["approved_providers"] if not lock["is_legacy"] else ()),
            project_decisions=tuple(lock["project_decisions"] if not lock["is_legacy"] else ()),
            drifted_needs=tuple(facts["drifted_needs"]),
        )

    change = proposed_change if isinstance(proposed_change, dict) else {}
    task_id = change.get("build_task_id")
    task_view = None
    if task_id:
        wanted = _canon(task_id) if isinstance(task_id, str) else None
        t = next((t for t in facts["tasks"] if t["id"] == wanted), None)
        if t:
            task_view = lock_check_rules.TaskView(
                id=t["id"], title=t["title"], status=t["status"],
                is_current=t["is_current"],
                permitted_paths=tuple(t["permitted_paths"]),
                prohibited_changes=tuple(t["prohibited_changes"]),
                drifted_needs=tuple(t["drifted_needs"]),
            )

    result = lock_check_rules.evaluate_change(
        lock=lock_view, task=task_view, task_requested=bool(task_id),
        change=proposed_change,
    )
    store.audit(
        conn, project_id=project_id, event_type="lock_check",
        subject_table="architecture_lock", subject_id=lock["id"] if lock else None,
        payload={
            "build_task_id": task_view.id if task_view else None,
            "lock_version": lock["version"] if lock else None,
            "verdict": result.verdict,
            "reasons": list(result.reasons),
            # An oversized change is refused, and is not stored either.
            "change": change if result.verdict != lock_check_rules.BLOCKED else {
                k: change[k] for k in ("build_task_id",) if isinstance(change.get(k), str)
            },
        },
    )
    conn.commit()
    return {
        "verdict": result.verdict,
        "reason": result.reason,
        "reasons": list(result.reasons),
        "cited": list(result.cited),
        "reminders": list(result.reminders),
        "lock_version": lock["version"] if lock else None,
        "next": {
            lock_check_rules.ALLOWED: None,
            lock_check_rules.NEEDS_APPROVAL:
                "Do not make this change yet. Re-decide what it touches "
                "(record_decision), then lock_architecture and wait for the human. "
                "The relock approval shows the diff; that is the change request.",
            lock_check_rules.BLOCKED:
                "Resolve what is blocking and check again.",
        }[result.verdict],
    }


# ---------------------------------------------------------------------------
# 11. record_build_progress
# ---------------------------------------------------------------------------

@_tool
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
    verification: dict[str, Any] | list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """
    Create a build task, or report progress and verification on one.

    Creating a task is the build gate: see core.pdr.tasks.task_gate.
    """
    bad = _bad_input(title, objective, req_ids, permitted_paths, prohibited_changes,
                     status, target_commit, verification)
    if bad:
        return bad
    if build_task_id:
        canonical = _canon(build_task_id)
        if not canonical:
            return {"error": f"Build task {one_line(build_task_id, 60)} not found"}
        return _update_build_task(
            conn, build_task_id=canonical, status=status,
            target_commit=target_commit, verification=verification,
        )
    return _create_build_task(
        conn,
        project_id=project_id,
        architecture_lock_id=architecture_lock_id,
        title=title, objective=objective,
        req_ids=req_ids,
        permitted_paths=permitted_paths,
        prohibited_changes=prohibited_changes,
        reused_capability_version_id=reused_capability_version_id,
    )


def _create_build_task(
    conn, *, project_id, architecture_lock_id, title, objective,
    req_ids, permitted_paths, prohibited_changes, reused_capability_version_id,
) -> dict[str, Any]:
    project_id = _canon(project_id)
    if not project_id:
        return {"error": "project_id is required"}
    if architecture_lock_id:
        architecture_lock_id = _canon(architecture_lock_id)
        if not architecture_lock_id:
            return {"error": "architecture_lock_id is not a UUID"}
    if not isinstance(title, str) or not isinstance(objective, str) \
            or not title.strip() or not objective.strip():
        return {"error": "title and objective are required"}
    if len(title) > MAX_TASK_TITLE_CHARS or len(objective) > MAX_RATIONALE_CHARS:
        return {"error": f"title is longer than {MAX_TASK_TITLE_CHARS} characters, or "
                         f"objective longer than {MAX_RATIONALE_CHARS}"}
    if not is_plain_line(title):
        # A title comes back inside lock-check reasons, refusals and status.
        return {"error": "title must be plain single-line text"}
    if not isinstance(req_ids, (list, tuple)) or not req_ids \
            or not all(isinstance(r, str) and r.strip() for r in req_ids):
        return {"error": "At least one req_id is required (a list of slugs or ids)"}
    if reused_capability_version_id:
        reused_capability_version_id = _canon(reused_capability_version_id)
        if not reused_capability_version_id:
            return {"error": "reused_capability_version_id is not a UUID"}
    prohibited, problem = task_rules.validate_text_list(prohibited_changes, "prohibited_changes")
    if problem:
        return {"error": problem}
    if not store.get_project(conn, project_id):
        return {"error": f"Project {project_id} not found"}

    facts = _facts(conn, project_id)
    lock = facts["lock"]
    if architecture_lock_id:
        if lock is None or lock["id"] != architecture_lock_id:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM architecture_lock WHERE id = %s AND project_id = %s",
                    (architecture_lock_id, project_id),
                )
                row = cur.fetchone()
            if not row:
                return {"error": "Architecture lock not found for this project"}
            return _task_refusal(conn, project_id, title, [
                f"architecture_lock_id is a {row[0]} lock; tasks are created under "
                f"the active lock"
                + (f" (v{lock['version']}, {lock['id']})" if lock else "")
            ])

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, slug, kind, status, disposition FROM project_requirement "
            "WHERE project_id = %s",
            (project_id,),
        )
        all_rows = cur.fetchall()
    by_id = {str(r[0]): r for r in all_rows}
    by_slug = {r[1]: r for r in all_rows}
    index = facts["index"]

    rows: list[task_rules.LinkedRow] = []
    unknown: list[str] = []
    seen_rows: set[str] = set()
    for ref in dict.fromkeys(r.strip() for r in req_ids):
        r = by_id.get(_canon(ref) or ref) or by_slug.get(ref)
        if r is None:
            unknown.append(ref)
            continue
        rid = str(r[0])
        if rid in seen_rows:
            continue                              # named once by slug and once by id
        seen_rows.add(rid)
        rows.append(task_rules.LinkedRow(
            id=rid, slug=r[1], kind=r[2], status=r[3], disposition=r[4],
            need_slugs=tuple((index.get(rid) or {}).get("need_slugs") or ()),
        ))
    if unknown:
        return {"error": "No such requirement in this project: "
                         + ", ".join(one_line(u, 60) for u in unknown)}

    locked = facts["locked_decisions"]
    need_id_by_slug = {n["slug"]: n["id"] for n in facts["scope"]["needs"]}
    current_ids = {
        slug: (facts["decisions"].get(nid) or {}).get("id")
        for slug, nid in need_id_by_slug.items()
    }
    build_gate: dict[str, tuple[bool, str]] = {}
    for slug, d in locked.items():
        if d["verdict"] == "BUILD" and slug in need_id_by_slug:
            decision = can_generate_for(
                conn, project_requirement_id=uuid.UUID(need_id_by_slug[slug]),
            )
            build_gate[slug] = (decision.allowed, decision.reason)

    refusals = task_rules.task_gate(
        has_active_lock=lock is not None,
        lock_stale_reasons=facts["stale_reasons"],
        locked_decisions=locked,
        current_decision_ids=current_ids,
        rows=rows,
        reused_capability_version_id=reused_capability_version_id,
        permitted_paths=permitted_paths,
        build_gate=build_gate,
    )
    if refusals:
        return _task_refusal(conn, project_id, title, refusals)

    reused = task_rules.resolve_reused_version(rows, locked, reused_capability_version_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO build_task
                (project_id, architecture_lock_id, title, objective,
                 permitted_paths, prohibited_changes, reused_capability_version_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                project_id, lock["id"], title.strip(), objective.strip(),
                [p.strip() for p in permitted_paths if p.strip()],
                prohibited, reused,
            ),
        )
        task_id = str(cur.fetchone()[0])
        for r in rows:
            cur.execute(
                "INSERT INTO build_task_requirement (build_task_id, project_requirement_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (task_id, r.id),
            )
    store.audit(
        conn, project_id=project_id, event_type="task_created",
        subject_table="build_task", subject_id=task_id,
        payload={"title": title, "lock_version": lock["version"],
                 "links": sorted(r.slug for r in rows)},
    )
    conn.commit()
    return {
        "build_task_id": task_id,
        "status": "planned",
        "architecture_lock_id": lock["id"],
        "lock_version": lock["version"],
        "links": sorted(r.slug for r in rows),
        "reused_capability_version_id": reused,
    }


def _task_refusal(
    conn, project_id: str, title: Any, reasons: list[str], **extra: Any,
) -> dict[str, Any]:
    """A refused task operation: nothing from the call is kept except the audit row."""
    conn.rollback()
    store.audit(
        conn, project_id=project_id, event_type="task_refused",
        payload={"title": one_line(title, 200), "reasons": reasons},
    )
    conn.commit()
    joined = "; ".join(reasons)
    return {"error": joined, "refused": True, "reason": joined, "reasons": reasons, **extra}


def _task_context(conn, build_task_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT project_id FROM build_task WHERE id = %s", (build_task_id,))
        row = cur.fetchone()
    if not row:
        return None
    project_id = str(row[0])
    facts = _facts(conn, project_id)
    task = next(t for t in facts["tasks"] if t["id"] == build_task_id)
    return {
        "project_id": project_id, "facts": facts, "task": task,
        "criteria": _task_criteria(task, facts["index"]),
    }


def _task_criteria(task: dict[str, Any], index: dict[str, dict]) -> list[dict[str, Any]]:
    """Acceptance criteria of the PDR requirements a task links."""
    return [
        {"id": c["id"], "ordinal": c["ordinal"], "text": c["text"],
         "requirement_slug": index[rid]["slug"], "requirement_id": rid}
        for rid in task["requirement_ids"]
        if rid in index and index[rid]["kind"] != req_rules.NEED_KIND
        for c in index[rid]["criteria"]
    ]


def _demote_lapsed_tasks(conn, project_id: str) -> list[str]:
    """
    Move every task stored as `verified` whose evidence no longer supports
    it back to `implemented`. Returns the ids demoted. Does not commit.

    `verified` is a stored flag, and the facts under it can move: a fail
    is recorded (by this task or another), a criterion is added, a
    criterion is rewritten and its evidence goes with it. Reads never
    trust the flag (see _facts); this makes the stored value agree.
    """
    facts = _facts(conn, project_id)
    lapsed = [t["id"] for t in facts["tasks"] if t.get("verification_lapsed")]
    if lapsed:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE build_task SET status = 'implemented', updated_at = now() "
                "WHERE id = ANY(%s::uuid[]) AND status = 'verified'",
                (lapsed,),
            )
        for tid in lapsed:
            store.audit(
                conn, project_id=project_id, event_type="task_updated",
                subject_table="build_task", subject_id=tid,
                payload={"from": "verified", "to": "implemented", "regressed": True},
            )
    return lapsed


def _abandon_stale_tasks(conn, project_id: str, version: int) -> list[str]:
    """
    Abandon every task the lock just written does not carry: one whose
    requirement left the build, or whose decision changed. Does not commit.

    A stale task could only ever be abandoned, but it was left for the
    agent to do, and a task that was never abandoned came back to life:
    defer its requirement and relock, bring the requirement back and
    relock again, and the task was current once more with the status and
    evidence it had two locks ago. A lock is the point at which the human
    says what the build is; work it does not include ends there.
    """
    facts = _facts(conn, project_id)
    stale = [t for t in facts["tasks"]
             if not t["is_current"] and t["stored_status"] != "abandoned"]
    for t in stale:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE build_task SET status = 'abandoned', updated_at = now() "
                "WHERE id = %s AND status <> 'abandoned'",
                (t["id"],),
            )
        store.audit(
            conn, project_id=project_id, event_type="task_updated",
            subject_table="build_task", subject_id=t["id"],
            payload={"from": t["stored_status"], "to": "abandoned",
                     "reason": f"not carried by lock v{version}"},
        )
    return [t["id"] for t in stale]


def _update_build_task(
    conn, *, build_task_id, status, target_commit, verification,
) -> dict[str, Any]:
    ctx = _task_context(conn, build_task_id)
    if ctx is None:
        return {"error": f"Build task {build_task_id} not found"}
    task, project_id = ctx["task"], ctx["project_id"]
    facts = ctx["facts"]
    if status is not None and not isinstance(status, str):
        return {"error": "status must be a string"}
    if target_commit is not None and not isinstance(target_commit, str):
        return {"error": "target_commit must be a string"}
    new_status = (status or "").strip() or None
    commit = (target_commit or "").strip() or None
    reports = verification if isinstance(verification, list) else (
        [verification] if verification else []
    )
    only_abandoning = new_status == "abandoned" and not reports and not commit

    if task["stored_status"] == "abandoned":
        if only_abandoning:
            return {"build_task_id": build_task_id, "status": "abandoned"}
        return _task_refusal(conn, project_id, task["title"], [
            "This task was abandoned. It takes no more progress or verification; "
            "create a new task."
        ])
    if not task["is_current"] and not only_abandoning:
        return _task_refusal(conn, project_id, task["title"], [
            "This task is stale: the lock it was created under is no longer one the "
            "workflow relies on (a decision it depends on changed, something it links "
            "was retired, or that lock was never approved). It can only be abandoned. "
            "Create a new task under the active lock."
        ])
    if facts["stale_reasons"] and not only_abandoning:
        # The scope is unconfirmed right now. Evidence recorded against it
        # would be evidence for criteria nobody has approved.
        return _task_refusal(conn, project_id, task["title"], [
            "The active lock is stale (" + "; ".join(facts["stale_reasons"]) + "). "
            "Progress and verification are not recorded against an unconfirmed "
            "scope. Reconfirm and relock first; a task can still be abandoned."
        ])
    if task["drifted_needs"] and not only_abandoning:
        # The same rule that stops a new task: the decision this work rests
        # on is no longer the locked one, so the lock does not cover it.
        return _task_refusal(conn, project_id, task["title"], [
            "The decision for " + ", ".join(f"'{s}'" for s in task["drifted_needs"])
            + " has changed since it was locked. Work that rests on it is on hold: "
              "relock (lock_architecture) first. The task can still be abandoned."
        ])
    if len(reports) > task_rules.MAX_REPORTS_PER_CALL:
        return {"error": f"at most {task_rules.MAX_REPORTS_PER_CALL} verification "
                         f"reports per call"}
    if (new_status or task["status"]) == "planned" and any(_reports_pass(r) for r in reports):
        # A pass is a claim about work. A task nobody has started has none,
        # and counting passes against it let a project read "verified" with
        # nothing implemented and no lock check ever expected. A fail is
        # recorded whatever the task's status: it is true either way.
        return _task_refusal(conn, project_id, task["title"], [
            "This task is still planned. Move it to in_progress (status='in_progress', "
            "in this call or before it) to record a pass against it."
        ])

    # task["status"] is the effective status: a stored `verified` whose
    # evidence has lapsed already reads as `implemented`.
    current_status = task["status"]
    if new_status:
        problem = task_rules.check_transition(current_status, new_status)
        if problem:
            return {"error": problem}
    if commit and not task_rules._COMMIT.match(commit):
        return {"error": "target_commit must be a git commit hash (7-64 hex characters)"}

    recorded: list[dict[str, Any]] = []
    for i, raw in enumerate(reports):
        clean, errors = task_rules.validate_verification(raw)
        if clean is None:
            conn.rollback()
            return {"error": f"verification[{i}]: " + "; ".join(errors)}
        row, err = _insert_verification(
            conn, ctx, clean, recorded_by="agent_reported",
        )
        if err:
            conn.rollback()
            return {"error": f"verification[{i}]: {err}"}
        recorded.append(row)

    # Evidence first, then status. Whatever was asked for, a task (this one
    # or any other) whose evidence no longer holds stops being verified.
    demoted = _demote_lapsed_tasks(conn, project_id)
    regressed = build_task_id in demoted
    if regressed or task.get("verification_lapsed"):
        current_status = "implemented"

    refusal = None
    if new_status == "verified":
        # The same evidence the reads use: what live tasks recorded, plus
        # every fail. `verified` was once granted here on a pass from a
        # task abandoned long before, while status and coverage said no.
        latest = _facts(conn, project_id)["latest_verifications"]
        refusal = task_rules.verified_refusal(
            task_id=build_task_id, criteria=ctx["criteria"], latest=latest,
        )
        if refusal:
            new_status = None

    sets, params = [], []
    if new_status and new_status != current_status:
        sets.append("status = %s")
        params.append(new_status)
    if commit:
        sets.append("target_commit = %s")
        params.append(commit)
    if sets:
        sets.append("updated_at = now()")
        params.append(build_task_id)
        with conn.cursor() as cur:
            cur.execute(f"UPDATE build_task SET {', '.join(sets)} WHERE id = %s", params)
        store.audit(
            conn, project_id=project_id, event_type="task_updated",
            subject_table="build_task", subject_id=build_task_id,
            payload={"from": current_status, "to": new_status or current_status,
                     "target_commit": commit},
        )
    if refusal:
        # Keep the verification rows just recorded: a report is evidence
        # whether or not it earns the status. Only the status is refused.
        store.audit(
            conn, project_id=project_id, event_type="task_refused",
            payload={"title": one_line(task["title"], 200), "gate": "verified",
                     "reasons": [refusal], "build_task_id": build_task_id,
                     "verifications_kept": len(recorded)},
        )
    conn.commit()

    final_status = new_status or current_status
    out: dict[str, Any] = {"build_task_id": build_task_id, "status": final_status}
    if recorded:
        out["verifications"] = recorded
    if refusal:
        reason = f"Cannot mark the task verified: {refusal}"
        out.update({"error": reason, "refused": True, "reason": reason, "reasons": [reason]})
    if regressed or (task.get("verification_lapsed") and final_status == "implemented"):
        out["note"] = (
            "A criterion's latest verification is no longer a pass, so the task "
            "moved from verified back to implemented."
        )
    if final_status in ("in_progress", "implemented") and not task["lock_checks"]:
        out["reminder"] = (
            "No check_against_lock call is on record for this task. Check before "
            "touching dependencies, providers, or files outside permitted_paths."
        )
    return out


def _reports_pass(raw: Any) -> bool:
    return isinstance(raw, dict) and isinstance(raw.get("outcome"), str) \
        and raw["outcome"].strip().lower() == "pass"


def _insert_verification(
    conn, ctx: dict[str, Any], clean: dict[str, Any], *, recorded_by: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve the criterion and write one requirement_verification row."""
    task = ctx["task"]
    criterion = None
    if clean["criterion_id"]:
        criterion = next((c for c in ctx["criteria"] if c["id"] == clean["criterion_id"]), None)
    else:
        criterion = next(
            (c for c in ctx["criteria"]
             if c["ordinal"] == clean["criterion_ordinal"]
             and clean["req_ref"] in (c["requirement_id"], c["requirement_slug"])),
            None,
        )
    if criterion is None:
        return None, (
            "that acceptance criterion does not belong to a requirement this task "
            "links. A task can only verify what it delivers."
        )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO requirement_verification
                (project_requirement_id, build_task_id, acceptance_criterion_id,
                 outcome, evidence_ref, commit_sha, test_ref, command, exit_code,
                 output_digest, worktree_dirty, recorded_by, verified_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp())
            RETURNING id
            """,
            (
                criterion["requirement_id"], task["id"], criterion["id"],
                clean["outcome"], clean["evidence_ref"], clean["commit_sha"],
                clean["test_ref"], clean["command"], clean["exit_code"],
                clean["output_digest"], clean["worktree_dirty"], recorded_by,
            ),
        )
        vid = str(cur.fetchone()[0])
    store.audit(
        conn, project_id=ctx["project_id"], event_type="verification_recorded",
        subject_table="requirement_verification", subject_id=vid,
        payload={"build_task_id": task["id"], "requirement": criterion["requirement_slug"],
                 "criterion": criterion["ordinal"], "outcome": clean["outcome"],
                 "recorded_by": recorded_by, "commit_sha": clean["commit_sha"]},
    )
    return {
        "verification_id": vid,
        "requirement": criterion["requirement_slug"],
        "criterion_ordinal": criterion["ordinal"],
        "outcome": clean["outcome"],
        "recorded_by": recorded_by,
    }, None


@_tool
def record_executed_verification(
    conn: psycopg.Connection,
    *,
    build_task_id: str,
    verification: dict[str, Any],
) -> dict[str, Any]:
    """
    Record a verification CIP observed itself. Called by `cip-composer
    verify`, which runs the command and fills in the exit code, output
    digest and commit; never exposed as an MCP tool.
    """
    canonical = _canon(build_task_id)
    if not canonical:
        return {"error": f"Build task {one_line(build_task_id, 60)} not found"}
    build_task_id = canonical
    bad = _bad_input(verification)
    if bad:
        return bad
    ctx = _task_context(conn, build_task_id)
    if ctx is None:
        return {"error": f"Build task {build_task_id} not found"}
    if ctx["task"]["stored_status"] == "abandoned":
        return {"error": "this task was abandoned; create a new task"}
    if not ctx["task"]["is_current"]:
        return {"error": "this task is stale; create a new task under the active lock"}
    if ctx["facts"]["stale_reasons"]:
        return {"error": "the active lock is stale ("
                         + "; ".join(ctx["facts"]["stale_reasons"])
                         + "); reconfirm and relock before recording verification"}
    if ctx["task"]["drifted_needs"]:
        return {"error": "the decision for "
                         + ", ".join(ctx["task"]["drifted_needs"])
                         + " has changed since it was locked; relock before recording "
                           "verification"}
    if ctx["task"]["status"] == "planned" and _reports_pass(verification):
        return {"error": "this task is still planned; move it to in_progress before "
                         "recording a pass against it"}
    clean, errors = task_rules.validate_verification(verification)
    if clean is None:
        return {"error": "; ".join(errors)}
    row, err = _insert_verification(conn, ctx, clean, recorded_by="cip_executed")
    if err:
        conn.rollback()
        return {"error": err}

    demoted = _demote_lapsed_tasks(conn, ctx["project_id"])
    if build_task_id in demoted:
        row["task_regressed_to"] = "implemented"
    conn.commit()
    # Judged on the same evidence as status and coverage: what live tasks
    # recorded. Otherwise this could say nothing remains while they do not.
    facts = _facts(conn, ctx["project_id"])
    row["remaining"] = task_rules.unverified_criteria(
        criteria=ctx["criteria"], latest=facts["latest_verifications"])
    return row


# ---------------------------------------------------------------------------
# Shared facts
# ---------------------------------------------------------------------------

def _facts(conn, project_id: str) -> dict[str, Any]:
    """
    The project's current state, loaded once per tool call.

    Each task carries its EFFECTIVE status. A task stored as `verified`
    reads as `implemented` (with verification_lapsed) unless every
    acceptance criterion it delivers has a passing latest verification.
    Nothing downstream — coverage, status, the lock check — takes the
    stored flag on trust.
    """
    source = store.latest_source(conn, project_id)
    scope = store.load_scope(conn, project_id)
    index = store.scope_index(scope)
    _, req_hash = store.scope_hash(scope, source)
    lock = store.load_active_lock(conn, project_id)
    # A lock is usable only while the scope it was approved for is the
    # confirmed scope. Matching hashes are not enough: see lock_stale_reasons.
    conf = store.confirmation(conn, project_id, req_hash)
    stale = store.lock_stale_reasons(lock, req_hash, conf["state"]) if lock else []
    locked = store.locked_decisions_by_slug(lock)
    decisions = store.current_decisions(conn, project_id)

    # Needs whose decision has moved since the lock froze it.
    drifted = {
        slug for slug, d in locked.items()
        if (decisions.get(d["need_id"]) or {}).get("id") != d["decision_record_id"]
    }

    lock_cache: dict[str, tuple[dict[str, dict], bool]] = {}
    tasks = store.load_tasks(conn, project_id)
    for t in tasks:
        lid = t["architecture_lock_id"]
        if lid not in lock_cache:
            its_lock = store.load_lock(conn, lid)
            lock_cache[lid] = (store.locked_decisions_by_slug(its_lock),
                               bool(its_lock and its_lock["is_legacy"]))
        t["is_current"] = store.task_is_current(
            t, active_lock=lock, task_lock_decisions=lock_cache[lid][0],
            scope_index=index, task_lock_is_legacy=lock_cache[lid][1],
        )
        t["stored_status"] = t["status"]
        t["drifted_needs"] = sorted(
            set(store.dependent_need_slugs(t, index)) & drifted
        )

    # Evidence counts while the work it was recorded against is still the
    # build: a task that is current, has been started and has not been
    # abandoned. A pass cannot be recorded on a planned task, and one
    # recorded before the task was moved back to planned does not count
    # while it sits there.
    live = [t["id"] for t in tasks
            if t["is_current"] and t["status"] not in ("abandoned", "planned")]
    latest = store.latest_verifications(conn, project_id, live)

    for t in tasks:
        t["verification_lapsed"] = False
        if t["status"] == "verified":
            criteria = _task_criteria(t, index)
            if (not t["is_current"] or not criteria
                    or task_rules.unverified_criteria(criteria=criteria, latest=latest)):
                t["status"] = "implemented"
                t["verification_lapsed"] = True
    return {
        "source": source,
        "scope": scope,
        "index": index,
        "requirements_hash": req_hash,
        "confirmation": conf,
        "lock": lock,
        "stale_reasons": stale,
        "locked_decisions": locked,
        "decisions": decisions,
        "drifted_needs": sorted(drifted),
        "tasks": tasks,
        "latest_verifications": latest,
    }


# ---------------------------------------------------------------------------
# 12. coverage
# ---------------------------------------------------------------------------

def coverage(
    conn: psycopg.Connection, *, project_id: str,
) -> dict[str, Any] | None:
    """
    How much of the PDR is built, and on what evidence. Writes nothing.
    See core.pdr.coverage for what each number is divided by.
    """
    project = store.get_project(conn, project_id)
    if not project:
        return None
    return _coverage_from(conn, project, _facts(conn, project["id"]))


def _coverage_from(conn, project: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    project_id = project["id"]
    scope, lock = facts["scope"], facts["lock"]
    latest = facts["latest_verifications"]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT rs.project_requirement_id FROM requirement_search rs "
            "JOIN project_requirement pr ON pr.id = rs.project_requirement_id "
            "WHERE pr.project_id = %s",
            (project_id,),
        )
        searched = {str(r[0]) for r in cur.fetchall()}

    tasks_by_req: dict[str, list[str]] = {}
    for t in facts["tasks"]:
        for rid in t["requirement_ids"]:
            tasks_by_req.setdefault(rid, []).append(t["id"])

    requirements = [
        {
            "id": r["id"], "slug": r["slug"], "kind": r["kind"],
            "priority": r["priority"], "disposition": r["disposition"],
            "need_slugs": r["need_slugs"],
            "criteria": [{"id": c["id"], "ordinal": c["ordinal"]} for c in r["criteria"]],
            "latest": {c["id"]: latest[c["id"]] for c in r["criteria"] if c["id"] in latest},
            "task_ids": tasks_by_req.get(r["id"], []),
        }
        for r in scope["requirements"]
    ]
    needs = []
    for n in scope["needs"]:
        d = facts["decisions"].get(n["id"])
        if d and d["need_fingerprint"] != n["fingerprint"]:
            d = None                      # decided against an older definition
        needs.append({
            "id": n["id"], "slug": n["slug"], "searched": n["id"] in searched,
            "decision": d,
        })

    result = coverage_rules.compute_coverage(
        span_coverage=store.scope_span_coverage(scope, facts["source"]),
        requirements=requirements,
        needs=needs,
        tasks=facts["tasks"],
        lock=({
            "version": lock["version"],
            "is_usable": not facts["stale_reasons"],
            "stale_reasons": facts["stale_reasons"],
            "decisions": facts["locked_decisions"],
        } if lock else None),
    )
    return {"project_id": project_id, "project_name": project["name"], **result}


# ---------------------------------------------------------------------------
# 13. pdr_status
# ---------------------------------------------------------------------------

def _lock_request_moved(conn, req: dict[str, Any]) -> str | None:
    """
    Why a lock request no longer describes a lock that could be written,
    or None if it still does. The request is replayed against the project
    as it is now; if the result is not the content that was requested, the
    summary the human would read (or did read) is about something else.
    """
    project = store.get_project(conn, req["project_id"])
    if project is None or not isinstance(req.get("subject"), dict):
        return "it no longer belongs to a project this version can read"
    prep = _prepare_lock(conn, project=project, replay=req["subject"])
    if prep["blockers"]:
        return "the project can no longer be locked as it stands: " + "; ".join(
            prep["blockers"][:3])
    if prep["payload_hash"] != req["subject_hash"]:
        return ("a decision, the requirements or the scope changed after it was "
                "requested (or the requirements were confirmed again), so it "
                "describes a lock that would no longer be written")
    return None


def _approved_unwritten_lock_request(conn, project_id: str, lock: dict | None) -> dict | None:
    """
    The lock request the human has approved that can be written now: it is
    the most recent lock request, it has not been used, and replaying it
    against the project as it stands gives exactly what was approved.
    Anything less is not "waiting to be written"; it is dead, and telling
    a fresh session to write it sends it into a refusal.
    """
    latest_id = pdr_approvals.latest_requested_lock_request(conn, project_id)
    if not latest_id:
        return None
    full = pdr_approvals.get_request(conn, latest_id)
    if (full is None or full["kind"] != "architecture_lock"
            or full["state"] != "approved" or full["project_id"] != project_id):
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM architecture_lock WHERE approval_id = %s", (full["approval_id"],),
        )
        if cur.fetchone():
            return None
    if _lock_request_moved(conn, full):
        return None
    return {"approval_request_id": full["id"], "approved_by": full["approver"],
            "decided_at": full["decided_at"]}


def pdr_status(
    conn: psycopg.Connection,
    *,
    project_id: str | None = None,
    project_name: str | None = None,
) -> dict[str, Any] | None:
    """
    Where a project is in the workflow and what is allowed next.

    The first call of any session. Everything a fresh context needs to
    resume: ids, phase, blockers, pending human approvals, an approved
    lock request waiting to be written, the active lock and what it
    froze, open tasks. Writes nothing.
    """
    project = None
    if project_id:
        project = store.get_project(conn, project_id)
    elif isinstance(project_name, str) and project_name.strip():
        project = store.find_project_by_name(conn, project_name.strip())
    if not project:
        return None
    pid = project["id"]

    facts = _facts(conn, pid)
    scope, source, lock = facts["scope"], facts["source"], facts["lock"]
    req_hash = facts["requirements_hash"]
    blockers = store.scope_blockers(scope, source)
    conf = store.confirmation(conn, pid, req_hash)
    cov = _coverage_from(conn, project, facts)

    decided_ids = {
        n["id"] for n in scope["needs"]
        if (facts["decisions"].get(n["id"]) or {}).get("need_fingerprint") == n["fingerprint"]
    }
    swept_ids = {
        n["id"] for n in scope["needs"]
        if any(store.counts_as_sweep(s, n["fingerprint"])
               for s in store.load_searches(conn, n["id"]))
    }

    pending_all = pdr_approvals.list_requests(conn, project_id=pid, state="pending")
    pending, obsolete = [], 0
    for p in pending_all:
        if request_obsolete_reason(conn, p, req_hash):
            obsolete += 1
        else:
            pending.append(p)
    approved_request = _approved_unwritten_lock_request(conn, pid, lock)
    latest_lock_request = next(
        (r for r in pdr_approvals.list_requests(conn, project_id=pid, state=None)
         if r["kind"] == "architecture_lock"
         and (r["subject"] or {}).get("replaces_lock_version")
         == (lock["version"] if lock else None)),
        None,
    )
    if lock and not facts["stale_reasons"]:
        lock_state = "active"
    elif approved_request:
        lock_state = "approved"
    elif any(p["kind"] == "architecture_lock" for p in pending):
        lock_state = "pending"
    elif lock:
        lock_state = "stale"
    elif latest_lock_request and latest_lock_request["state"] == "rejected":
        lock_state = "rejected"
    else:
        lock_state = "none"

    phase, next_actions = phase_rules.derive_phase(
        has_source=source is not None,
        requirement_count=len(scope["requirements"]),
        confirmation_blockers=blockers,
        confirmation_state=conf["state"],
        needs_total=len(scope["needs"]),
        needs_searched=len(swept_ids),
        needs_decided=len(decided_ids),
        lock_state=lock_state,
        in_scope=cov["progress"]["in_scope"],
        verified=cov["progress"]["verified"],
        approved_lock_request_id=(approved_request or {}).get("approval_request_id"),
    )

    drifted = sorted(
        slug for slug, d in facts["locked_decisions"].items()
        if (facts["decisions"].get(d["need_id"]) or {}).get("id") != d["decision_record_id"]
    )
    if phase == "building" and drifted:
        next_actions = [
            "decisions changed since the lock for: " + ", ".join(drifted)
            + " — relock (lock_architecture) to apply them; tasks on those needs "
              "are refused until then",
        ] + next_actions

    if approved_request and lock_state == "active":
        # A relock was approved while the current lock is still usable.
        next_actions = [
            "an approved relock is waiting to be written: lock_architecture(project_id, "
            f"approval_request_id='{approved_request['approval_request_id']}')",
        ] + next_actions
    elif lock_state == "active" and any(p["kind"] == "architecture_lock" for p in pending):
        next_actions = [
            "a relock request is waiting for the human (`cip-composer approvals`); "
            "the current lock stays in force until it is approved and written",
        ] + next_actions

    usable_lock = lock and not lock["is_legacy"]
    return {
        "project_id": pid,
        "project_name": project["name"],
        "phase": phase,
        "next_actions": next_actions,
        "pdr": {
            "requirement_source_id": source["id"] if source else None,
            "title": source["title"] if source else None,
            "source": source["raw_ref"] if source else None,
            "content_hash": source["content_hash"] if source else None,
            "text_stored": bool(source and source["raw_text"] is not None),
            "supersedes_id": source["supersedes_id"] if source else None,
        },
        "requirements": {
            "hash": req_hash,
            "confirmation": conf["state"],
            "confirmed_by": conf["approver"] if conf["state"] == "approved" else None,
            "blockers": blockers,
            "items": [
                {"req_id": r["id"], "slug": r["slug"], "kind": r["kind"],
                 "priority": r["priority"], "disposition": r["disposition"],
                 "needs": r["need_slugs"],
                 "criteria": [{"criterion_id": c["id"], "ordinal": c["ordinal"],
                               "text": c["text"]} for c in r["criteria"]]}
                for r in scope["requirements"]
            ],
        },
        "needs": [
            {
                "need_id": n["id"], "slug": n["slug"], "satisfies": n["satisfies"],
                "swept": n["id"] in swept_ids,
                "decided": n["id"] in decided_ids,
                "verdict": (facts["decisions"][n["id"]]["final_verdict"]
                            if n["id"] in decided_ids else None),
                "decided_by": (facts["decisions"][n["id"]]["decided_by"]
                               if n["id"] in decided_ids else None),
                "in_lock": n["slug"] in facts["locked_decisions"]
                           and n["slug"] not in drifted,
            }
            for n in scope["needs"]
        ],
        "pending_approvals": [
            {"approval_request_id": p["id"], "kind": p["kind"],
             "requested_at": p["requested_at"],
             "how_to_approve": pdr_approvals.approve_hint(p["id"])}
            for p in pending
        ],
        "obsolete_requests": obsolete,
        "approved_lock_request": approved_request,
        "lock": ({
            "architecture_lock_id": lock["id"],
            "version": lock["version"],
            "approved_by": lock["approved_by"],
            "approved_at": lock["approved_at"],
            "usable": not facts["stale_reasons"],
            "stale_reasons": facts["stale_reasons"],
            "drifted_needs": drifted,
            "decisions": [
                {"need": d["need_slug"], "verdict": d["verdict"],
                 "capability_key": d.get("capability_key"),
                 "capability_version_id": d.get("capability_version_id"),
                 "pinned_revision": d.get("pinned_revision")}
                for d in (lock["decisions"] if usable_lock else [])
            ],
            "approved_providers": lock["approved_providers"] if usable_lock else [],
            "cost_commitments": lock["cost_commitments"] if usable_lock else [],
            "project_decisions": lock["project_decisions"] if usable_lock else [],
            "unresolved_risks": lock["unresolved_risks"] if usable_lock else [],
            "architecture_doc_ref": lock["architecture_doc_ref"],
        } if lock else None),
        "tasks": [
            {"build_task_id": t["id"], "title": t["title"], "status": t["status"],
             "current": t["is_current"], "lock_checks": t["lock_checks"],
             "permitted_paths": t["permitted_paths"],
             "reused_capability_version_id": t["reused_capability_version_id"],
             "links": sorted(facts["index"][rid]["slug"]
                             for rid in t["requirement_ids"] if rid in facts["index"])}
            for t in facts["tasks"] if t["status"] != "abandoned"
        ],
        "progress": cov["progress"],
        "compliance": cov["compliance"],
    }


# ---------------------------------------------------------------------------
# For the human's CLI: describe a request, with what a person should check
# ---------------------------------------------------------------------------

def describe_request(conn: psycopg.Connection, request_id: str) -> dict[str, Any] | None:
    """
    An approval request plus the checks a human needs before deciding:
    whether it is obsolete, and — for a requirements confirmation — whether
    the PDR file on disk still matches what was ingested.
    """
    req = pdr_approvals.get_request(conn, request_id)
    if req is None:
        return None
    req["obsolete_reason"] = request_obsolete_reason(conn, req)
    req["warnings"] = []
    if req["kind"] == "requirements_confirmation" and req["state"] == "pending":
        req["source_check"] = _source_file_check(
            store.latest_source(conn, req["project_id"])
        )
    elif req["kind"] == "architecture_lock" and req["state"] == "pending":
        subject = req["subject"] if isinstance(req["subject"], dict) else {}
        req["doc_check"] = _file_check(
            subject.get("architecture_doc_ref"), subject.get("architecture_doc_file_hash"),
            then="read for this lock request",
            changed="The lock would record the earlier content, not what the file says now.",
        )
    return req


def request_obsolete_reason(
    conn: psycopg.Connection, req: dict[str, Any], current_hash: str | None = None,
) -> str | None:
    """
    Why a pending approval request no longer corresponds to anything, or
    None. One definition, used by pdr_status, the CLI's list, and the
    approve prompt, so they cannot disagree about what is waiting.

      - history moved on (another confirmation was approved, a newer lock
        exists, or a later lock request was made): pdr_approvals
      - the requirements were edited after a confirmation was requested
      - the project changed after a lock was requested, so the request
        describes a lock that would no longer be written
    """
    if req["state"] != "pending":
        return None
    reason = pdr_approvals.obsolete_reason(conn, req)
    if reason:
        return reason
    if req["kind"] == "requirements_confirmation":
        if current_hash is None:
            source = store.latest_source(conn, req["project_id"])
            scope = store.load_scope(conn, req["project_id"])
            _, current_hash = store.scope_hash(scope, source)
        if (req["subject"] or {}).get("requirements_hash") != current_hash:
            return "the requirements have been edited since this request was made"
        return None
    return _lock_request_moved(conn, req)


def _source_file_check(source: dict[str, Any] | None) -> str | None:
    """Compare the PDR file on disk with the hash recorded at ingest."""
    if not source:
        return None
    return _file_check(
        source.get("raw_ref"), source.get("source_file_hash"),
        then="ingested",
        changed="The requirements below were extracted from the earlier text.",
    )


def _file_check(ref: Any, recorded_hash: Any, *, then: str, changed: str) -> str | None:
    """
    Compare a document on disk with the SHA-256 recorded for it. None when
    there is no file to compare (inline text, or nothing recorded).
    """
    if not isinstance(ref, str) or not ref or ref == "inline" or not recorded_hash:
        return None
    try:
        data = Path(ref).read_bytes()
    except OSError:
        return f"could not open {ref} to compare it with what was {then}"
    if hashlib.sha256(data).hexdigest() == recorded_hash:
        return f"{ref} on disk matches what was {then}"
    return f"WARNING: {ref} has changed since it was {then}. {changed}"
