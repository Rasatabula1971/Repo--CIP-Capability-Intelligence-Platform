"""
Build tasks — the gate on starting one, status transitions, and what
"verified" means.

Task gate. v1.0 gated recording a verdict. The thing that needs gating
is creating a build_task, because that is the moment code starts being
written. A task may start only when everything it links to is covered
by the active, current, human-approved lock, and — for anything that
means writing new code against a BUILD decision — Step 13's
can_generate_for says yes.

Verified. A requirement is verified when every one of its acceptance
criteria has a latest verification of `pass`. Latest wins: a `fail`
recorded after a `pass` un-verifies the criterion, the requirement, and
any task that was marked verified.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import re

from core.pdr.lock_check import path_pattern_problem

STATUSES = ("planned", "in_progress", "implemented", "verified", "abandoned")

_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "planned":     ("in_progress", "abandoned"),
    "in_progress": ("implemented", "planned", "abandoned"),
    "implemented": ("verified", "in_progress", "abandoned"),
    "verified":    ("implemented", "abandoned"),
    "abandoned":   (),
}

DEPENDENCY_VERDICTS = ("ADOPT", "ADAPT", "WRAP")
OUTCOMES = ("pass", "fail", "not_run")

_COMMIT = re.compile(r"^[0-9a-fA-F]{7,64}$")
_INT64 = 2 ** 63
MAX_PATHS = 100
MAX_REPORTS_PER_CALL = 100


def check_transition(old: str, new: str) -> str | None:
    """None if the move is legal, else the reason it is not."""
    if new not in STATUSES:
        return f"status must be one of {list(STATUSES)}, got '{new}'"
    if new == old:
        return None
    if new not in _TRANSITIONS.get(old, ()):
        allowed = ", ".join(_TRANSITIONS.get(old, ())) or "nothing (terminal)"
        return f"cannot move a task from '{old}' to '{new}'; from '{old}' the legal moves are: {allowed}"
    return None


# ---------------------------------------------------------------------------
# Task gate
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LinkedRow:
    """One project_requirement row a task wants to link."""
    id: str
    slug: str
    kind: str
    status: str
    disposition: str | None
    need_slugs: tuple[str, ...] = ()      # for a via_needs requirement


def task_gate(
    *,
    has_active_lock: bool,
    lock_stale_reasons: list[str],
    locked_decisions: dict[str, dict[str, Any]],     # need slug -> decision
    current_decision_ids: dict[str, str | None],      # need slug -> latest decision_record id
    rows: list[LinkedRow],
    reused_capability_version_id: str | None,
    permitted_paths: list[str] | None,
    build_gate: dict[str, tuple[bool, str]],          # need slug -> Step 13 (allowed, reason)
) -> list[str]:
    """Reasons a task may not be created. Empty list means it may."""
    refusals: list[str] = []

    if not has_active_lock:
        return ["No active architecture lock. Lock the decisions before creating build tasks."]
    for r in lock_stale_reasons:
        refusals.append(f"The active lock is stale: {r}")

    if not rows:
        return refusals + ["A task must link at least one requirement."]

    if permitted_paths is not None and not isinstance(permitted_paths, (list, tuple)):
        refusals.append("permitted_paths must be a list of paths.")
        permitted_paths = []
    if not permitted_paths:
        refusals.append(
            "permitted_paths is required: the files or directories this task may touch. "
            "Without it check_against_lock has nothing to hold the task to."
        )
    elif len(permitted_paths) > MAX_PATHS:
        refusals.append(f"at most {MAX_PATHS} permitted_paths.")
    for p in (permitted_paths or [])[:MAX_PATHS]:
        problem = path_pattern_problem(p)
        if problem:
            refusals.append(f"permitted_paths entry {p!r} {problem}.")

    deliverables = [r for r in rows if r.kind != "capability_need"]
    needs = [r for r in rows if r.kind == "capability_need"]
    if not deliverables:
        refusals.append(
            "A task must link at least one PDR requirement (what it delivers); "
            "capability needs alone carry no acceptance criteria to verify."
        )

    need_slugs_to_check: dict[str, str] = {n.slug: "linked directly" for n in needs}
    for r in rows:
        if r.status != "active":
            refusals.append(f"'{r.slug}' is retired.")
    for r in deliverables:
        if r.disposition is None:
            refusals.append(f"Requirement '{r.slug}' has no disposition.")
        elif r.disposition in ("deferred", "not_applicable"):
            refusals.append(
                f"Requirement '{r.slug}' is {r.disposition}; it is not in this build."
            )
        elif r.disposition == "via_needs":
            if not r.need_slugs:
                refusals.append(f"Requirement '{r.slug}' is via_needs but has no capability need.")
            for s in r.need_slugs:
                need_slugs_to_check.setdefault(s, f"required by '{r.slug}'")

    for slug in sorted(need_slugs_to_check):
        why = need_slugs_to_check[slug]
        locked = locked_decisions.get(slug)
        if locked is None:
            refusals.append(
                f"Capability need '{slug}' ({why}) is not in the active lock. "
                f"Search, decide and relock before building on it."
            )
            continue
        if current_decision_ids.get(slug) != locked.get("decision_record_id"):
            refusals.append(
                f"The decision for '{slug}' ({why}) has changed since it was locked. "
                f"Relock before building on it."
            )
            continue
        if locked["verdict"] == "BUILD":
            allowed, reason = build_gate.get(slug, (False, "no build-gate result"))
            if not allowed:
                refusals.append(
                    f"Search-before-build gate refuses '{slug}': {reason}"
                )

    # A task that rests on a reused capability, whether it links the need
    # directly or reaches it through a requirement, may only name the
    # version the lock pinned.
    pinned = reusable_versions(need_slugs_to_check, locked_decisions)
    if reused_capability_version_id and reused_capability_version_id not in pinned:
        if pinned:
            refusals.append(
                f"reused_capability_version_id {reused_capability_version_id} is not what "
                f"the lock pinned for this task's needs ({', '.join(sorted(pinned))})."
            )
        else:
            refusals.append(
                f"reused_capability_version_id {reused_capability_version_id} does not "
                f"belong to a reuse decision this task rests on."
            )

    return refusals


def reusable_versions(
    need_slugs, locked_decisions: dict[str, dict[str, Any]],
) -> set[str]:
    """Locked capability versions among the needs a task rests on."""
    return {
        locked_decisions[s]["capability_version_id"]
        for s in need_slugs
        if s in locked_decisions
        and locked_decisions[s].get("verdict") in DEPENDENCY_VERDICTS
        and locked_decisions[s].get("capability_version_id")
    }


def dependent_need_slugs(rows: list[LinkedRow]) -> list[str]:
    """Needs a task rests on: linked directly, or behind a linked requirement."""
    slugs: set[str] = set()
    for r in rows:
        if r.kind == "capability_need":
            slugs.add(r.slug)
        elif r.disposition == "via_needs":
            slugs.update(r.need_slugs)
    return sorted(slugs)


def resolve_reused_version(
    rows: list[LinkedRow],
    locked_decisions: dict[str, dict[str, Any]],
    given: str | None,
) -> str | None:
    """
    The version to store on the task. When the task rests on exactly one
    reused capability the answer is determined by the lock, so CIP fills
    it in rather than relying on the agent to echo it back.
    """
    allowed = reusable_versions(dependent_need_slugs(rows), locked_decisions)
    if given:
        return given
    return next(iter(allowed)) if len(allowed) == 1 else None


def validate_text_list(value: Any, what: str, max_items: int = 50,
                       max_chars: int = 300) -> tuple[list[str], str | None]:
    """A list of short strings, or the reason it is not one."""
    if value is None:
        return [], None
    if not isinstance(value, (list, tuple)):
        return [], f"{what} must be a list of strings"
    if not all(isinstance(v, str) for v in value):
        return [], f"{what} must contain only strings"
    if len(value) > max_items:
        return [], f"at most {max_items} {what}"
    if any(len(v) > max_chars for v in value):
        return [], f"each {what} entry must be at most {max_chars} characters"
    return [v.strip() for v in value if v.strip()], None


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def validate_verification(raw: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """
    Validate one verification report. A `pass` must say what was run,
    against which commit, and that it exited 0. `fail` and `not_run`
    need only the criterion.
    """
    if not isinstance(raw, dict):
        return None, ["verification must be an object"]
    errors: list[str] = []
    outcome = _s(raw.get("outcome")).lower()
    if outcome not in OUTCOMES:
        errors.append(f"outcome must be one of {list(OUTCOMES)}")

    criterion_id = _s(raw.get("criterion_id")) or None
    ordinal = raw.get("criterion_ordinal")
    req_ref = _s(raw.get("req_id")) or _s(raw.get("req_slug")) or None
    if not criterion_id and not (req_ref and isinstance(ordinal, int)):
        errors.append(
            "name the acceptance criterion: criterion_id, or req_id with criterion_ordinal"
        )

    commit = _s(raw.get("commit_sha")) or None
    test_ref = _s(raw.get("test_ref")) or None
    command = _s(raw.get("command")) or None
    exit_code = raw.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        exit_code = None
    elif not -_INT64 <= exit_code < _INT64:
        errors.append("exit_code is out of range")
        exit_code = None
    if commit and not _COMMIT.match(commit):
        errors.append("commit_sha must be a git commit hash (7-64 hex characters)")
    digest = _s(raw.get("output_digest")) or None
    evidence_ref = _s(raw.get("evidence_ref")) or None
    for label, value, limit in (("test_ref", test_ref, 500), ("command", command, 2000),
                                ("output_digest", digest, 128), ("evidence_ref", evidence_ref, 500)):
        if value and len(value) > limit:
            errors.append(f"{label} is longer than {limit} characters")

    if outcome == "pass":
        if not commit:
            errors.append("a pass needs commit_sha (the commit the test ran against)")
        if not test_ref:
            errors.append("a pass needs test_ref (e.g. tests/test_auth.py::test_login)")
        if not command:
            errors.append("a pass needs command (what was run)")
        if exit_code != 0:
            errors.append("a pass needs exit_code 0")
    if errors:
        return None, errors
    return {
        "outcome": outcome,
        "criterion_id": criterion_id,
        "criterion_ordinal": ordinal if isinstance(ordinal, int) else None,
        "req_ref": req_ref,
        "commit_sha": commit,
        "test_ref": test_ref,
        "command": command,
        "exit_code": exit_code,
        "output_digest": digest,
        "evidence_ref": evidence_ref,
        "worktree_dirty": raw.get("worktree_dirty") if isinstance(raw.get("worktree_dirty"), bool) else None,
    }, []


def unverified_criteria(
    *,
    criteria: list[dict[str, Any]],            # {id, requirement_slug, ordinal, text}
    latest: dict[str, dict[str, Any]],         # criterion id -> {outcome, build_task_id, recorded_by}
) -> list[str]:
    """Human-readable list of criteria whose latest verification is not a pass."""
    out: list[str] = []
    for c in sorted(criteria, key=lambda c: (c["requirement_slug"], c["ordinal"])):
        v = latest.get(c["id"])
        state = v["outcome"] if v else "no verification recorded"
        if state != "pass":
            out.append(f"{c['requirement_slug']} #{c['ordinal']} ({state}): {c['text']}")
    return out


def verified_refusal(
    *,
    task_id: str,
    criteria: list[dict[str, Any]],
    latest: dict[str, dict[str, Any]],
) -> str | None:
    """None if the task may be marked verified, else why not."""
    if not criteria:
        return (
            "the task's requirements have no acceptance criteria, so there is "
            "nothing to verify it against"
        )
    missing = unverified_criteria(criteria=criteria, latest=latest)
    if missing:
        return "acceptance criteria without a passing verification: " + "; ".join(missing)
    if not any(v.get("build_task_id") == task_id for v in latest.values()):
        return "no passing verification was recorded against this task"
    return None


def _s(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""
