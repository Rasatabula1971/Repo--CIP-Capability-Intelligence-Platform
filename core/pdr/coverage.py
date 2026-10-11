"""
Coverage — "how much of the PDR is built, and on what evidence?"

v1.0 divided by the number of extracted requirements, so a PDR that was
70% extracted reported 100%. The denominators here are explicit:

  pdr           sections of the source text with no requirement mapped
  requirements  what is in scope, deferred, not applicable, undecided
  needs         what the registry could answer, and who decided
  progress      in-scope requirements decided → locked → tasked →
                implemented → verified
  compliance    signals that the workflow was actually followed

`verified` is split by evidence source. `agent_reported` means the
coding agent said the test passed. `cip_executed` means
`cip-composer verify` ran a command, saw it exit 0, and the working tree
was clean, so the recorded commit is what was tested. Neither proves
the command tests the criterion: the command and test reference are
returned per requirement so a human can read them.
"""
from __future__ import annotations

from typing import Any

from core.pdr.decisions import DEPENDENCY_VERDICTS
from core.pdr.requirements import IN_SCOPE_DISPOSITIONS


def compute_coverage(
    *,
    span_coverage: dict[str, Any] | None,
    requirements: list[dict[str, Any]],
    needs: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    lock: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    requirements (active, non-need): {id, slug, kind, priority, disposition,
        need_slugs[], criteria[{id, ordinal}], latest{criterion_id: {outcome,
        recorded_by}}, task_ids[]}
    needs (active): {id, slug, searched, decision{final_verdict, decided_by,
        coverage_outcome, id} | None}
    tasks: {id, status, is_current, lock_checks}
    lock: {version, is_usable, stale_reasons[], decisions{need slug: {...}}} | None
    """
    need_by_slug = {n["slug"]: n for n in needs}
    task_by_id = {t["id"]: t for t in tasks}
    locked = (lock or {}).get("decisions") or {}
    lock_usable = bool(lock and lock.get("is_usable"))

    by_requirement: list[dict[str, Any]] = []
    counts = {k: 0 for k in (
        "in_scope", "decided", "locked", "tasked", "implemented",
        "verified", "verified_cip_executed",
    )}
    by_kind: dict[str, int] = {}
    by_disposition: dict[str, int] = {}

    for r in sorted(requirements, key=lambda r: r["slug"]):
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        disp = r.get("disposition") or "undispositioned"
        by_disposition[disp] = by_disposition.get(disp, 0) + 1
        in_scope = r.get("disposition") in IN_SCOPE_DISPOSITIONS

        linked = [need_by_slug[s] for s in r.get("need_slugs") or [] if s in need_by_slug]
        if r.get("disposition") == "bespoke":
            decided = True
            is_locked = lock_usable
        elif r.get("disposition") == "via_needs":
            decided = bool(linked) and all(n.get("decision") for n in linked)
            is_locked = decided and lock_usable and all(
                (locked.get(n["slug"]) or {}).get("decision_record_id") == n["decision"]["id"]
                for n in linked
            )
        else:
            decided = is_locked = False

        live = [
            task_by_id[t] for t in r.get("task_ids") or []
            if t in task_by_id and task_by_id[t]["is_current"]
            and task_by_id[t]["status"] != "abandoned"
        ]
        # Each stage implies the one before it. A requirement is not
        # "verified" because its tests passed once: it is verified when the
        # decision it rests on is the locked one, a live task has reported
        # it implemented, and every criterion's latest evidence is a pass.
        # Without the chain a project read "complete" with every task still
        # planned, or after a locked ADOPT had been re-decided as BUILD.
        tasked = is_locked and bool(live)
        implemented = tasked and any(t["status"] in ("implemented", "verified") for t in live)

        criteria = r.get("criteria") or []
        latest = r.get("latest") or {}
        outcomes = [(latest.get(c["id"]) or {}).get("outcome") for c in criteria]
        criteria_pass = bool(criteria) and all(o == "pass" for o in outcomes)
        verified = implemented and criteria_pass
        cip_executed = verified and all(
            (latest.get(c["id"]) or {}).get("recorded_by") == "cip_executed"
            # A pass on a dirty tree did not test the commit it names.
            and not (latest.get(c["id"]) or {}).get("worktree_dirty")
            for c in criteria
        )
        evidence = [
            {
                "criterion_ordinal": c["ordinal"],
                "outcome": (latest.get(c["id"]) or {}).get("outcome"),
                "recorded_by": (latest.get(c["id"]) or {}).get("recorded_by"),
                "command": (latest.get(c["id"]) or {}).get("command"),
                "test_ref": (latest.get(c["id"]) or {}).get("test_ref"),
                "commit_sha": (latest.get(c["id"]) or {}).get("commit_sha"),
                "worktree_dirty": (latest.get(c["id"]) or {}).get("worktree_dirty"),
                # For a CIP-executed run, the repository it ran in. A pass
                # recorded in some other repository shows here.
                "evidence_ref": (latest.get(c["id"]) or {}).get("evidence_ref"),
            }
            for c in sorted(criteria, key=lambda c: c["ordinal"])
        ]

        if in_scope:
            counts["in_scope"] += 1
            counts["decided"] += decided
            counts["locked"] += is_locked
            counts["tasked"] += tasked
            counts["implemented"] += implemented
            counts["verified"] += verified
            counts["verified_cip_executed"] += cip_executed

        verdicts = sorted({n["decision"]["final_verdict"] for n in linked if n.get("decision")})
        by_requirement.append({
            "req_id": r["id"],
            "slug": r["slug"],
            "kind": r["kind"],
            "priority": r["priority"],
            "disposition": r.get("disposition"),
            "needs": [n["slug"] for n in linked],
            "verdict": verdicts[0] if len(verdicts) == 1 else (None if not verdicts else "MIXED"),
            "verdicts": verdicts,
            "decided": decided,
            "locked": is_locked,
            "task_status": _best_status(live),
            "criteria_total": len(criteria),
            "criteria_passed": sum(1 for o in outcomes if o == "pass"),
            "tasked": tasked,
            "implemented": implemented,
            "verified": verified,
            # What the evidence says on its own, whatever stage the work is at.
            "verification_outcome": (
                "pass" if criteria_pass else ("fail" if "fail" in outcomes else
                                              ("partial" if "pass" in outcomes else None))
            ),
            "verified_by": ("cip_executed" if cip_executed else
                            ("agent_reported" if verified else None)),
            "evidence": evidence,
        })

    decided_needs = [n for n in needs if n.get("decision")]
    by_verdict: dict[str, int] = {}
    by_decided_by: dict[str, int] = {}
    by_outcome: dict[str, int] = {}
    for n in decided_needs:
        d = n["decision"]
        by_verdict[d["final_verdict"]] = by_verdict.get(d["final_verdict"], 0) + 1
        by_decided_by[d["decided_by"]] = by_decided_by.get(d["decided_by"], 0) + 1
        by_outcome[d["coverage_outcome"]] = by_outcome.get(d["coverage_outcome"], 0) + 1
    # Reuse means the capability becomes part of the build. REFERENCE is
    # reading something and then building; it is counted on its own.
    reuse = sum(v for k, v in by_verdict.items() if k in DEPENDENCY_VERDICTS)
    answered = by_outcome.get("candidates_evaluated", 0)

    current_tasks = [t for t in tasks if t["is_current"] and t["status"] != "abandoned"]
    worked = [t for t in current_tasks if t["status"] in ("in_progress", "implemented", "verified")]

    return {
        "pdr": {
            "section_count": (span_coverage or {}).get("section_count"),
            "mapped_section_count": (span_coverage or {}).get("mapped_section_count"),
            "unmapped_sections": (span_coverage or {}).get("unmapped_sections", []),
            "out_of_scope_only_sections": (span_coverage or {}).get(
                "out_of_scope_only_sections", []),
            "lightly_quoted_sections": (span_coverage or {}).get(
                "lightly_quoted_sections", []),
            "quoted_char_pct": (span_coverage or {}).get("quoted_char_pct"),
        },
        "requirements": {
            "total": len(requirements),
            "by_kind": dict(sorted(by_kind.items())),
            "by_disposition": dict(sorted(by_disposition.items())),
            "in_scope": counts["in_scope"],
        },
        "needs": {
            "total": len(needs),
            "searched": sum(1 for n in needs if n.get("searched")),
            "decided": len(decided_needs),
            "by_verdict": dict(sorted(by_verdict.items())),
            "by_decided_by": dict(sorted(by_decided_by.items())),
            "by_coverage_outcome": dict(sorted(by_outcome.items())),
            "reuse_pct": _pct(reuse, len(decided_needs)),
            "reference_count": by_verdict.get("REFERENCE", 0),
            "registry_answered_pct": _pct(answered, len(decided_needs)),
        },
        "progress": {
            **counts,
            "verified_agent_reported_only": counts["verified"] - counts["verified_cip_executed"],
            "verified_pct": _pct(counts["verified"], counts["in_scope"]),
        },
        "compliance": {
            "lock_version": (lock or {}).get("version"),
            "lock_usable": lock_usable,
            "lock_stale_reasons": (lock or {}).get("stale_reasons", []),
            "tasks_total": len(tasks),
            "tasks_stale": sum(1 for t in tasks if not t["is_current"] and t["status"] != "abandoned"),
            "tasks_worked_without_lock_check": sum(1 for t in worked if not t.get("lock_checks")),
        },
        # v1.0 keys, kept so existing callers do not break. `total_requirements`
        # now counts in-scope requirements — the honest denominator.
        "total_requirements": counts["in_scope"],
        "decided": counts["decided"],
        "tasked": counts["tasked"],
        "implemented": counts["implemented"],
        "verified": counts["verified"],
        "by_requirement": by_requirement,
    }


def _best_status(tasks: list[dict[str, Any]]) -> str | None:
    order = ("verified", "implemented", "in_progress", "planned")
    present = {t["status"] for t in tasks}
    for s in order:
        if s in present:
            return s
    return None


def _pct(part: int, whole: int) -> int | None:
    return int(round(100 * part / whole)) if whole else None
