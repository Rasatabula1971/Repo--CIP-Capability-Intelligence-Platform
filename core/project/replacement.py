"""
Failure-driven replacement engine (Phase 6).

When a component fails in a project, this module:
  1. Diagnoses the failure — maps failure_kind to actionable categories
  2. Determines if replacement is warranted based on severity + history
  3. Builds search criteria for finding alternatives
  4. Ranks alternatives by excluding the failed component

Pure domain logic — no DB imports. The queries module handles persistence.

Diagnosis categories:
  compatibility  — install/import/build failures, dependency conflicts
  reliability    — runtime errors, performance degradation
  security       — vulnerabilities
  api_change     — breaking API changes
  other          — unclassifiable

Replacement strategy:
  same_kind      — search for same component_kind + ecosystem
  broader        — relax to any component_kind in same ecosystem
  cross_eco      — search across ecosystems
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DiagnosisCategory(str, Enum):
    COMPATIBILITY = "compatibility"
    RELIABILITY = "reliability"
    SECURITY = "security"
    API_CHANGE = "api_change"
    OTHER = "other"


class ReplacementStrategy(str, Enum):
    SAME_KIND = "same_kind"
    BROADER = "broader"
    CROSS_ECO = "cross_eco"


class ReplacementUrgency(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


_FAILURE_TO_CATEGORY = {
    "install_failure": DiagnosisCategory.COMPATIBILITY,
    "import_failure": DiagnosisCategory.COMPATIBILITY,
    "build_failure": DiagnosisCategory.COMPATIBILITY,
    "dependency_conflict": DiagnosisCategory.COMPATIBILITY,
    "runtime_error": DiagnosisCategory.RELIABILITY,
    "performance_degradation": DiagnosisCategory.RELIABILITY,
    "test_failure": DiagnosisCategory.RELIABILITY,
    "security_vulnerability": DiagnosisCategory.SECURITY,
    "api_breaking_change": DiagnosisCategory.API_CHANGE,
    "other": DiagnosisCategory.OTHER,
}

_SEVERITY_TO_URGENCY = {
    "warning": ReplacementUrgency.LOW,
    "error": ReplacementUrgency.MEDIUM,
    "critical": ReplacementUrgency.CRITICAL,
}

_CATEGORY_URGENCY_BOOST = {
    DiagnosisCategory.SECURITY: ReplacementUrgency.HIGH,
    DiagnosisCategory.COMPATIBILITY: ReplacementUrgency.MEDIUM,
}


@dataclass(frozen=True)
class FailureDiagnosis:
    failure_kind: str
    category: str
    urgency: str
    root_cause_hint: str
    recommended_strategy: str


@dataclass(frozen=True)
class ReplacementCriteria:
    exclude_ids: list[str]
    ecosystem: str | None
    component_kind: str | None
    search_terms: list[str]
    strategy: str


@dataclass(frozen=True)
class ReplacementCandidate:
    capability_id: str
    normalized_key: str
    display_name: str
    score: float
    reason: str


@dataclass(frozen=True)
class ReplacementPlan:
    failed_capability_id: str
    diagnosis: FailureDiagnosis
    criteria: ReplacementCriteria
    candidates: list[ReplacementCandidate]
    recommendation: str


def diagnose_failure(
    failure_kind: str,
    severity: str = "error",
    summary: str = "",
    failure_history: list[dict[str, Any]] | None = None,
) -> FailureDiagnosis:
    category = _FAILURE_TO_CATEGORY.get(
        failure_kind, DiagnosisCategory.OTHER,
    )

    base_urgency = _SEVERITY_TO_URGENCY.get(
        severity, ReplacementUrgency.MEDIUM,
    )
    boosted = _CATEGORY_URGENCY_BOOST.get(category)
    if boosted and _urgency_rank(boosted) > _urgency_rank(base_urgency):
        urgency = boosted
    else:
        urgency = base_urgency

    if failure_history:
        repeat_count = sum(
            1 for f in failure_history
            if f.get("failure_kind") == failure_kind
        )
        if repeat_count >= 3:
            urgency = ReplacementUrgency.HIGH
        if repeat_count >= 5:
            urgency = ReplacementUrgency.CRITICAL

    root_cause_hint = _infer_root_cause(failure_kind, summary)
    strategy = _pick_strategy(category, urgency)

    return FailureDiagnosis(
        failure_kind=failure_kind,
        category=category.value if isinstance(category, DiagnosisCategory) else category,
        urgency=urgency.value if isinstance(urgency, ReplacementUrgency) else urgency,
        root_cause_hint=root_cause_hint,
        recommended_strategy=strategy.value if isinstance(strategy, ReplacementStrategy) else strategy,
    )


def build_replacement_criteria(
    failed_capability: dict[str, Any],
    diagnosis: FailureDiagnosis,
    additional_excludes: list[str] | None = None,
) -> ReplacementCriteria:
    exclude_ids = [failed_capability.get("id", "")]
    if additional_excludes:
        exclude_ids.extend(additional_excludes)

    ecosystem = failed_capability.get("ecosystem")
    component_kind = failed_capability.get("component_kind")

    display_name = failed_capability.get("display_name", "")
    normalized_key = failed_capability.get("normalized_key", "")
    terms = _extract_search_terms(display_name, normalized_key)

    strategy = diagnosis.recommended_strategy

    if strategy == ReplacementStrategy.BROADER.value:
        component_kind = None
    elif strategy == ReplacementStrategy.CROSS_ECO.value:
        ecosystem = None
        component_kind = None

    return ReplacementCriteria(
        exclude_ids=exclude_ids,
        ecosystem=ecosystem,
        component_kind=component_kind,
        search_terms=terms,
        strategy=strategy,
    )


def rank_candidates(
    candidates: list[dict[str, Any]],
    exclude_ids: list[str],
    project_scores: dict[str, float] | None = None,
) -> list[ReplacementCandidate]:
    filtered = [
        c for c in candidates
        if c.get("id") not in exclude_ids
    ]

    ranked: list[ReplacementCandidate] = []
    for c in filtered:
        cap_id = c.get("id", "")
        base_score = float(c.get("total_score") or 0.0)

        exp_bonus = 0.0
        if project_scores and cap_id in project_scores:
            exp_bonus = project_scores[cap_id] * 0.2

        final_score = base_score + exp_bonus

        reason = "registry score"
        if exp_bonus > 0:
            reason = "registry score + positive project experience"

        ranked.append(ReplacementCandidate(
            capability_id=cap_id,
            normalized_key=c.get("normalized_key", ""),
            display_name=c.get("display_name", ""),
            score=round(final_score, 4),
            reason=reason,
        ))

    ranked.sort(key=lambda x: x.score, reverse=True)
    return ranked


def build_replacement_plan(
    failed_capability: dict[str, Any],
    diagnosis: FailureDiagnosis,
    candidates: list[ReplacementCandidate],
) -> ReplacementPlan:
    criteria = build_replacement_criteria(failed_capability, diagnosis)

    if not candidates:
        recommendation = "no_alternatives_found"
    elif diagnosis.urgency in ("critical", "high"):
        recommendation = "replace_immediately"
    elif diagnosis.urgency == "medium":
        recommendation = "replace_soon"
    else:
        recommendation = "monitor_and_plan"

    return ReplacementPlan(
        failed_capability_id=failed_capability.get("id", ""),
        diagnosis=diagnosis,
        criteria=criteria,
        candidates=candidates,
        recommendation=recommendation,
    )


def _urgency_rank(u: ReplacementUrgency | str) -> int:
    order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    val = u.value if isinstance(u, ReplacementUrgency) else u
    return order.get(val, 0)


def _infer_root_cause(failure_kind: str, summary: str) -> str:
    hints = {
        "install_failure": "package may not be available or has incompatible dependencies",
        "import_failure": "package installed but module path may be wrong or has missing native deps",
        "build_failure": "build system misconfiguration or missing build tools",
        "dependency_conflict": "two or more packages require incompatible versions of a shared dep",
        "runtime_error": "bug or environmental incompatibility at runtime",
        "performance_degradation": "resource usage exceeds acceptable thresholds",
        "security_vulnerability": "known CVE or security advisory applies to this version",
        "api_breaking_change": "upstream API changed in a way that breaks integration",
        "test_failure": "component passes install but fails functional tests",
    }
    hint = hints.get(failure_kind, "unclassified failure")
    if summary:
        hint += f" — {summary}"
    return hint


def _pick_strategy(
    category: DiagnosisCategory, urgency: ReplacementUrgency | str,
) -> ReplacementStrategy:
    urgency_val = urgency.value if isinstance(urgency, ReplacementUrgency) else urgency
    if category == DiagnosisCategory.SECURITY and urgency_val == "critical":
        return ReplacementStrategy.CROSS_ECO
    if category == DiagnosisCategory.COMPATIBILITY:
        return ReplacementStrategy.SAME_KIND
    if urgency_val in ("high", "critical"):
        return ReplacementStrategy.BROADER
    return ReplacementStrategy.SAME_KIND


def _extract_search_terms(display_name: str, normalized_key: str) -> list[str]:
    terms: list[str] = []
    if display_name:
        words = display_name.replace("-", " ").replace("_", " ").split()
        terms.extend(w.lower() for w in words if len(w) > 2)
    if normalized_key:
        parts = normalized_key.split(":")
        if len(parts) > 1:
            name_part = parts[-1]
            words = name_part.replace("-", " ").replace("_", " ").split()
            for w in words:
                wl = w.lower()
                if wl not in terms and len(wl) > 2:
                    terms.append(wl)
    return terms[:5]
