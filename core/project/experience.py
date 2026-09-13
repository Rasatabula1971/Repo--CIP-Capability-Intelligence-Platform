"""
Project experience memory (Phase 5).

Pure domain logic for recording and evaluating component usage within
projects. Tracks adoption, failures, and computes experience scores
that feed back into future recommendations.

Experience score formula:
  base = 1.0 if any successful use, else 0.0
  penalty = total_failures / max(total_uses, 1) * FAILURE_WEIGHT
  recency = bonus if last_success_at is recent
  score = clamp(base - penalty + recency, 0.0, 1.0)

This module contains no DB imports — it operates on plain dicts and
dataclasses. The queries module handles persistence.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


FAILURE_WEIGHT = 0.5
RECENCY_BONUS = 0.1
RECENCY_DAYS = 30


class UseStatus(str, Enum):
    ACTIVE = "active"
    RETIRED = "retired"
    REPLACED = "replaced"
    FAILED = "failed"


class FailureKind(str, Enum):
    INSTALL_FAILURE = "install_failure"
    IMPORT_FAILURE = "import_failure"
    RUNTIME_ERROR = "runtime_error"
    PERFORMANCE_DEGRADATION = "performance_degradation"
    SECURITY_VULNERABILITY = "security_vulnerability"
    API_BREAKING_CHANGE = "api_breaking_change"
    DEPENDENCY_CONFLICT = "dependency_conflict"
    BUILD_FAILURE = "build_failure"
    TEST_FAILURE = "test_failure"
    OTHER = "other"


class FailureSeverity(str, Enum):
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass(frozen=True)
class ExperienceScoreInput:
    total_uses: int = 0
    total_failures: int = 0
    last_failure_at: datetime.datetime | None = None
    last_success_at: datetime.datetime | None = None


@dataclass(frozen=True)
class ExperienceScoreResult:
    score: float
    success_rate: float
    total_uses: int
    total_failures: int


def compute_experience_score(
    inp: ExperienceScoreInput,
    now: datetime.datetime | None = None,
) -> ExperienceScoreResult:
    if inp.total_uses == 0:
        return ExperienceScoreResult(
            score=0.0,
            success_rate=1.0,
            total_uses=0,
            total_failures=0,
        )

    success_rate = max(0.0, 1.0 - inp.total_failures / inp.total_uses)

    base = 1.0 if inp.total_uses > inp.total_failures else 0.0
    penalty = (inp.total_failures / inp.total_uses) * FAILURE_WEIGHT

    recency = 0.0
    if inp.last_success_at and now:
        days_since = (now - inp.last_success_at).total_seconds() / 86400
        if days_since <= RECENCY_DAYS:
            recency = RECENCY_BONUS

    score = max(0.0, min(1.0, base - penalty + recency))

    return ExperienceScoreResult(
        score=round(score, 4),
        success_rate=round(success_rate, 4),
        total_uses=inp.total_uses,
        total_failures=inp.total_failures,
    )


def should_recommend_replacement(
    score: ExperienceScoreResult,
    threshold: float = 0.3,
) -> bool:
    if score.total_uses == 0:
        return False
    return score.score < threshold


@dataclass(frozen=True)
class UsageSummary:
    capability_id: str
    project_id: str
    active_uses: int
    total_failures: int
    unresolved_failures: int
    experience_score: float
    recommendation: str


def summarize_usage(
    uses: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    score: ExperienceScoreResult | None,
    capability_id: str,
    project_id: str,
) -> UsageSummary:
    active_uses = sum(1 for u in uses if u.get("status") == "active")
    unresolved = sum(1 for f in failures if f.get("resolved_at") is None)

    exp_score = score.score if score else 0.0

    if active_uses == 0 and not uses:
        recommendation = "not_used"
    elif exp_score >= 0.7:
        recommendation = "continue"
    elif exp_score >= 0.3:
        recommendation = "monitor"
    else:
        recommendation = "consider_replacement"

    return UsageSummary(
        capability_id=capability_id,
        project_id=project_id,
        active_uses=active_uses,
        total_failures=len(failures),
        unresolved_failures=unresolved,
        experience_score=exp_score,
        recommendation=recommendation,
    )
