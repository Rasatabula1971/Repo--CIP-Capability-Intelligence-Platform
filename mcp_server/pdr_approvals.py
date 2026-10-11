"""
Human approval gates for the PDR-to-Build workflow.

An approval_request freezes exactly what is put in front of a human — a
confirmed requirement set, or a full architecture-lock payload — along
with its hash. A gate passes only when an approval exists for the hash
of the CURRENT content. Change anything after approval and the hash
moves, so the approval no longer applies.

An approval is also tied to a point in history, so it cannot be replayed:

  requirements   The subject names the approval it follows. The gate is
                 open only while the LATEST approved confirmation is for
                 the current content. Go back to content that was approved
                 earlier and it must be approved again.
  lock           The payload names the lock version it replaces. Once a
                 newer lock exists, an older approval matches nothing.

Who can write what:
  - ensure_request() is called by the workflow tools. Opening a request
    asserts nothing; it only asks.
  - record_approval() is called by ONE caller: the `cip-composer
    approvals` CLI, after it has confirmed an interactive terminal and a
    typed confirmation code. No MCP tool calls it.

This is governed cooperation, not a sandbox. An agent with a shell that
is determined to approve its own work can import this module and call
record_approval(). What the design buys is that it cannot happen by
drift: there is no tool for it, the CLI refuses a non-interactive
caller, and every approval names an OS user and a channel.
"""
from __future__ import annotations

import json
import re
from typing import Any

import psycopg

from core.pdr.canonical import content_hash
from core.pdr.text import one_line

KINDS = ("requirements_confirmation", "architecture_lock")
REQUIREMENTS_SUBJECT_SCHEMA = "requirements-confirmation/1"
APPROVE_HINT = (
    "A human must approve this in a terminal: "
    "`cip-composer approvals show {id}` then `cip-composer approvals approve {id}`. "
    "The agent must not run the approve command."
)

# A request is addressed by its UUID or a prefix of it. Only hex and
# dashes: the value is compared, never pattern-matched, so "%" or "_"
# cannot select "the only request there is".
_ID_PREFIX = re.compile(r"^[0-9a-f][0-9a-f-]{5,35}$")
MIN_PREFIX = 6


def ensure_request(
    conn,
    *,
    project_id: str,
    kind: str,
    subject: dict[str, Any],
    summary: str,
) -> dict[str, Any]:
    """
    Open an approval request for `subject`, or return the existing one
    for the same content. Idempotent on (project, kind, subject hash).
    Does not commit.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown approval kind: {kind}")
    subject_hash = content_hash(subject)
    # SELECT-then-INSERT, not ON CONFLICT: Postgres refuses ON CONFLICT on a
    # table that carries an UPDATE rule, and approval_request is append-only
    # by rule. The UNIQUE constraint still backstops a race.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM approval_request "
            "WHERE project_id = %s AND kind = %s AND subject_hash = %s",
            (project_id, kind, subject_hash),
        )
        row = cur.fetchone()
        created = row is None
        if created:
            cur.execute(
                """
                INSERT INTO approval_request (project_id, kind, subject_hash, subject, summary)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                RETURNING id
                """,
                (project_id, kind, subject_hash, json.dumps(subject), summary),
            )
            row = cur.fetchone()
    return {"id": str(row[0]), "created": created, "subject_hash": subject_hash}


def _state_row(row) -> dict[str, Any]:
    if row is None:
        return {"state": "none", "request_id": None, "approval_id": None,
                "approver": None, "note": None, "decided_at": None}
    request_id, approval_id, outcome, approver, note, decided_at, channel = row
    return {
        "state": outcome if outcome else "pending",
        "request_id": str(request_id),
        "approval_id": str(approval_id) if approval_id else None,
        "approver": approver,
        "note": note,
        "channel": channel,
        "decided_at": decided_at.isoformat() if decided_at else None,
    }


def request_state(
    conn, *, project_id: str, kind: str, subject_hash: str,
) -> dict[str, Any]:
    """
    State of the request for this exact content:
      none | pending | approved | rejected
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ar.id, a.id, a.outcome, a.approver, a.note, a.decided_at, a.channel
            FROM approval_request ar
            LEFT JOIN approval a ON a.approval_request_id = ar.id
            WHERE ar.project_id = %s AND ar.kind = %s AND ar.subject_hash = %s
            """,
            (project_id, kind, subject_hash),
        )
        return _state_row(cur.fetchone())


def latest_approved(conn, *, project_id: str, kind: str) -> dict[str, Any] | None:
    """The most recently approved request of a kind for a project."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ar.id, a.id, ar.subject, ar.subject_hash, a.approver, a.decided_at
            FROM approval a
            JOIN approval_request ar ON ar.id = a.approval_request_id
            WHERE ar.project_id = %s AND ar.kind = %s AND a.outcome = 'approved'
            ORDER BY a.decided_at DESC, a.id DESC
            LIMIT 1
            """,
            (project_id, kind),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "request_id": str(row[0]), "approval_id": str(row[1]), "subject": row[2],
        "subject_hash": row[3], "approver": row[4], "decided_at": row[5].isoformat(),
    }


# ---------------------------------------------------------------------------
# Requirements confirmation
# ---------------------------------------------------------------------------

def requirements_subject(
    *, requirements_hash: str, snapshot: dict[str, Any], follows_approval_id: str | None,
) -> dict[str, Any]:
    return {
        "schema": REQUIREMENTS_SUBJECT_SCHEMA,
        "requirements_hash": requirements_hash,
        "follows_approval_id": follows_approval_id,
        "snapshot": snapshot,
    }


def confirmation_state(conn, *, project_id: str, requirements_hash: str) -> dict[str, Any]:
    """
    Whether the current scope is confirmed.

    approved only when the LATEST approved confirmation for the project is
    for exactly this content. Otherwise the state of the request that
    would follow that approval: none, pending or rejected.
    """
    latest = latest_approved(conn, project_id=project_id, kind="requirements_confirmation")
    if latest and (latest["subject"] or {}).get("requirements_hash") == requirements_hash:
        return {
            "state": "approved", "request_id": latest["request_id"],
            "approval_id": latest["approval_id"], "approver": latest["approver"],
            "note": None, "decided_at": latest["decided_at"],
            "follows_approval_id": (latest["subject"] or {}).get("follows_approval_id"),
        }
    follows = latest["approval_id"] if latest else None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ar.id, a.id, a.outcome, a.approver, a.note, a.decided_at, a.channel
            FROM approval_request ar
            LEFT JOIN approval a ON a.approval_request_id = ar.id
            WHERE ar.project_id = %s AND ar.kind = 'requirements_confirmation'
              AND ar.subject->>'requirements_hash' = %s
              AND ar.subject->>'follows_approval_id' IS NOT DISTINCT FROM %s
            """,
            (project_id, requirements_hash, follows),
        )
        state = _state_row(cur.fetchone())
    state["follows_approval_id"] = follows
    return state


# ---------------------------------------------------------------------------
# Listing and lookup
# ---------------------------------------------------------------------------

def list_requests(
    conn, *, project_id: str | None = None, state: str | None = "pending",
) -> list[dict[str, Any]]:
    where, params = ["TRUE"], []
    if project_id:
        where.append("ar.project_id = %s")
        params.append(project_id)
    if state == "pending":
        where.append("a.id IS NULL")
    elif state in ("approved", "rejected"):
        where.append("a.outcome = %s")
        params.append(state)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT ar.id, ar.project_id, p.name, ar.kind, ar.subject_hash,
                   ar.requested_at, a.outcome, a.approver, a.decided_at, ar.subject
            FROM approval_request ar
            JOIN project p ON p.id = ar.project_id
            LEFT JOIN approval a ON a.approval_request_id = ar.id
            WHERE {' AND '.join(where)}
            ORDER BY ar.requested_at DESC, ar.id
            """,
            params,
        )
        rows = cur.fetchall()
    return [
        {
            "id": str(r[0]), "project_id": str(r[1]), "project_name": r[2],
            "kind": r[3], "subject_hash": r[4],
            "requested_at": r[5].isoformat(),
            "state": r[6] or "pending", "approver": r[7],
            "decided_at": r[8].isoformat() if r[8] else None,
            "subject": r[9],
        }
        for r in rows
    ]


def get_request(conn, request_id: str) -> dict[str, Any] | None:
    """
    One request by UUID or a unique prefix of at least MIN_PREFIX
    characters. None if nothing or more than one matches.
    """
    ref = (request_id or "").strip().lower() if isinstance(request_id, str) else ""
    if not _ID_PREFIX.match(ref):
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ar.id, ar.project_id, p.name, ar.kind, ar.subject_hash,
                   ar.subject, ar.summary, ar.requested_at,
                   a.outcome, a.approver, a.note, a.decided_at, a.id
            FROM approval_request ar
            JOIN project p ON p.id = ar.project_id
            LEFT JOIN approval a ON a.approval_request_id = ar.id
            WHERE left(ar.id::text, %s) = %s
            """,
            (len(ref), ref),
        )
        rows = cur.fetchall()
    if len(rows) != 1:
        return None
    r = rows[0]
    return {
        "id": str(r[0]), "project_id": str(r[1]), "project_name": r[2],
        "kind": r[3], "subject_hash": r[4], "subject": r[5], "summary": r[6],
        "requested_at": r[7].isoformat(),
        "state": r[8] or "pending", "approver": r[9], "note": r[10],
        "decided_at": r[11].isoformat() if r[11] else None,
        "approval_id": str(r[12]) if r[12] else None,
    }


def obsolete_reason(conn, req: dict[str, Any]) -> str | None:
    """
    Why a PENDING request no longer corresponds to anything, or None.

    A request is made against a point in history. If history has moved —
    another confirmation was approved, or a newer lock exists — approving
    it would approve a summary that no longer describes what would happen.
    """
    if req["state"] != "pending":
        return None
    subject = req["subject"] or {}
    if req["kind"] == "requirements_confirmation":
        latest = latest_approved(
            conn, project_id=req["project_id"], kind="requirements_confirmation",
        )
        follows = latest["approval_id"] if latest else None
        if subject.get("follows_approval_id") != follows:
            return (
                "another requirements confirmation has been approved since this "
                "request was made"
            )
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version FROM architecture_lock WHERE project_id = %s AND status = 'active'",
            (req["project_id"],),
        )
        row = cur.fetchone()
    active = row[0] if row else None
    if subject.get("replaces_lock_version") != active:
        was = subject.get("replaces_lock_version")
        return (
            f"it was made to replace {'lock v' + str(was) if was else 'no lock'}, "
            f"and the active lock is now {'v' + str(active) if active else 'none'}; "
            f"its summary and diff are out of date"
        )
    # One live lock request per project: the one asked for most recently.
    # Otherwise every variation an agent tries stays approvable for good,
    # and the person at the terminal has to work out which is meant.
    latest = latest_requested_lock_request(conn, req["project_id"])
    if latest is not None and latest != req["id"]:
        return "a different lock request has been made since; only the latest one can be approved"
    return None


def latest_requested_lock_request(conn, project_id: str) -> str | None:
    """
    The lock request asked for most recently, by its 'lock_approval_requested'
    audit event.

    Ordered by audit_event.seq, the order the events were written in, not
    by created_at. created_at is now(), which is when the writing
    transaction STARTED: a call that began first and finished last was the
    latest request with the earliest timestamp, so the request the human
    had just been shown was reported as superseded, and asking for it
    again returned the same dead request.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT subject_id FROM audit_event
            WHERE project_id = %s AND event_type = 'lock_approval_requested'
              AND subject_id IS NOT NULL
            ORDER BY seq DESC
            LIMIT 1
            """,
            (project_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def record_approval(
    conn,
    *,
    request_id: str,
    outcome: str,
    approver: str,
    note: str | None = None,
    channel: str = "cli_tty",
) -> dict[str, Any]:
    """
    Record a human decision on a request. One decision per request, ever:
    to change your mind, change the content, which opens a new request.

    Refuses a request that is obsolete (history moved on since it was
    made) and one whose stored subject no longer hashes to subject_hash —
    an approval is for the content shown, not for a row id. Commits.
    """
    if outcome not in ("approved", "rejected"):
        return {"error": "outcome must be 'approved' or 'rejected'"}
    if not isinstance(approver, str) or not approver.strip():
        return {"error": "approver must be non-empty"}
    # The note is typed by a person, and shown again later: on the lock, in
    # status, in a refusal. Keep it one clean line like everything else.
    if note is not None and not isinstance(note, str):
        return {"error": "note must be text"}
    note = one_line(note, 1000) or None
    approver = one_line(approver, 200) if isinstance(approver, str) else approver
    if outcome == "rejected" and not note:
        return {"error": "a rejection needs a note saying what to change"}

    req = get_request(conn, request_id)
    if req is None:
        return {"error": f"approval request {request_id!r} not found (give the full id "
                         f"or a unique prefix of at least {MIN_PREFIX} characters)"}
    if req["state"] != "pending":
        return {"error": f"request already {req['state']} by {req['approver']}"}
    if content_hash(req["subject"]) != req["subject_hash"]:
        return {"error": "stored request content does not match its hash; refusing to record"}
    stale = obsolete_reason(conn, req)
    if stale and outcome == "approved":
        return {"error": f"this request is obsolete: {stale}. Ask for a fresh request."}

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO approval
                    (approval_request_id, outcome, approver, channel, note, decided_at)
                VALUES (%s, %s, %s, %s, %s, clock_timestamp())
                RETURNING id, decided_at
                """,
                (req["id"], outcome, approver.strip(), channel, note),
            )
            approval_id, decided_at = cur.fetchone()
    except psycopg.errors.UniqueViolation:
        # Two terminals decided the same request at the same moment. One
        # decision per request: the other one stands.
        conn.rollback()
        return {"error": "this request was decided a moment ago, in another terminal"}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_event
                (project_id, event_type, actor, subject_table, subject_id, payload)
            VALUES (%s, %s, %s, 'approval_request', %s, %s::jsonb)
            """,
            (
                req["project_id"], f"approval_{outcome}", approver.strip(), req["id"],
                json.dumps({"kind": req["kind"], "subject_hash": req["subject_hash"],
                            "channel": channel, "note": note}),
            ),
        )
    conn.commit()
    return {
        "approval_id": str(approval_id),
        "approval_request_id": req["id"],
        "kind": req["kind"],
        "outcome": outcome,
        "approver": approver.strip(),
        "decided_at": decided_at.isoformat(),
    }


def approve_hint(request_id: str) -> str:
    return APPROVE_HINT.format(id=request_id[:8])
