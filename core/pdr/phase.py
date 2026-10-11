"""
Workflow phase and next actions — what pdr_status reports.

A build runs over many sessions. A fresh session has none of the
earlier context, so it needs one call that says where the project is
and what the workflow will accept next. This is derived from facts on
every call; nothing here is stored.
"""
from __future__ import annotations

from typing import Any

PHASES = (
    "no_pdr",
    "requirements_draft",
    "awaiting_requirements_confirmation",
    "search_and_decide",
    "ready_to_lock",
    "awaiting_lock_approval",
    "building",
    "relock_required",
    "complete",
)


def derive_phase(
    *,
    has_source: bool,
    requirement_count: int,
    confirmation_blockers: list[str],
    confirmation_state: str,           # none | pending | approved | rejected
    needs_total: int,
    needs_searched: int,
    needs_decided: int,
    lock_state: str,                   # none | pending | approved | rejected | active | stale
    approved_lock_request_id: str | None = None,
    in_scope: int,
    verified: int,
) -> tuple[str, list[str]]:
    """Return (phase, next_actions)."""
    if not has_source:
        return "no_pdr", ["ingest_pdr: register the project and store the PDR text"]

    if requirement_count == 0:
        return "requirements_draft", [
            "record_requirements: extract atomic requirements, each with a verbatim quote",
        ]

    if confirmation_state != "approved":
        if confirmation_blockers:
            return "requirements_draft", [
                "resolve the confirmation blockers (see `blockers`)",
                "record_capability_needs: derive the reusable capabilities from the "
                "architecture sketch and link them to requirements",
                "then request_requirements_confirmation",
            ]
        if confirmation_state == "pending":
            return "awaiting_requirements_confirmation", [
                "WAIT for the human: `cip-composer approvals` in a terminal. "
                "Do not run that command yourself.",
            ]
        if confirmation_state == "rejected":
            return "requirements_draft", [
                "the human rejected this requirement set; revise it and "
                "request_requirements_confirmation again",
            ]
        return "requirements_draft", ["request_requirements_confirmation"]

    if lock_state == "active":
        if in_scope and verified >= in_scope:
            return "complete", ["all in-scope requirements are verified"]
        return "building", [
            "record_build_progress: create tasks, then report status and verification",
            "check_against_lock before changing a dependency, a provider, or a file "
            "outside a task's permitted_paths",
            "`cip-composer verify` to have CIP run a criterion's test itself",
        ]

    if lock_state == "stale":
        return "relock_required", [
            "the active lock no longer matches the project (see `lock.stale_reasons`)",
            "re-decide what changed, then lock_architecture and wait for approval",
        ]

    if needs_decided < needs_total:
        actions = []
        if needs_searched < needs_total:
            actions.append("search_for_requirement for each capability need not yet searched")
        actions.append("record_decision for each searched need")
        return "search_and_decide", actions

    if lock_state == "approved":
        return "ready_to_lock", [
            "the human has approved the lock request; write it with "
            f"lock_architecture(project_id, approval_request_id='{approved_lock_request_id}'). "
            "Do not re-type the inputs: that call replays exactly what was approved.",
        ]
    if lock_state == "pending":
        return "awaiting_lock_approval", [
            "WAIT for the human: `cip-composer approvals` in a terminal. "
            "Do not run that command yourself.",
            "after approval, pdr_status will show the approved request id; "
            "pass it to lock_architecture as approval_request_id",
        ]
    if lock_state == "rejected":
        return "ready_to_lock", [
            "the human rejected the last lock request; change what was objected to, "
            "then lock_architecture again",
        ]
    return "ready_to_lock", [
        "pdr_approval_brief to preview, then lock_architecture with the architecture "
        "document, approved providers, cost commitments and cross-cutting decisions",
    ]
