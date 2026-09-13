"""
Phase 5 — Project Experience Memory tests.

Coverage:
  1. Experience score computation (pure function):
     - Zero uses → score 0
     - All successes → score 1.0
     - Some failures → reduced score
     - All failures → score 0
     - Recency bonus for recent success
     - No recency bonus when stale
  2. Usage summarizer (pure function):
     - Not used → 'not_used'
     - High score → 'continue'
     - Medium score → 'monitor'
     - Low score → 'consider_replacement'
     - Counts unresolved failures
  3. should_recommend_replacement (pure function):
     - Below threshold → True
     - Above threshold → False
     - Zero uses → False
  4. DB-backed queries:
     - record_component_use stores and returns use
     - retire_component_use changes status
     - log_failure_event stores failure
     - resolve_failure_event marks resolved
     - experience_summary aggregates data
     - experience_summary with no uses
     - experience_summary updates score on read
     - bad ids return None
"""
from __future__ import annotations

import datetime
import uuid

import pytest

from core.project.experience import (
    ExperienceScoreInput,
    ExperienceScoreResult,
    FailureKind,
    FailureSeverity,
    UseStatus,
    compute_experience_score,
    should_recommend_replacement,
    summarize_usage,
)
from mcp_server import queries


# ---------------------------------------------------------------------------
# Experience score computation — pure function tests
# ---------------------------------------------------------------------------

class TestComputeExperienceScore:

    def test_zero_uses(self):
        result = compute_experience_score(ExperienceScoreInput())
        assert result.score == 0.0
        assert result.success_rate == 1.0
        assert result.total_uses == 0

    def test_all_successes(self):
        inp = ExperienceScoreInput(total_uses=5, total_failures=0)
        result = compute_experience_score(inp)
        assert result.score == 1.0
        assert result.success_rate == 1.0

    def test_some_failures(self):
        inp = ExperienceScoreInput(total_uses=10, total_failures=3)
        result = compute_experience_score(inp)
        assert 0.0 < result.score < 1.0
        assert result.success_rate == 0.7

    def test_all_failures(self):
        inp = ExperienceScoreInput(total_uses=5, total_failures=5)
        result = compute_experience_score(inp)
        assert result.score == 0.0
        assert result.success_rate == 0.0

    def test_recency_bonus(self):
        now = datetime.datetime(2026, 9, 11, tzinfo=datetime.timezone.utc)
        recent = now - datetime.timedelta(days=5)
        inp = ExperienceScoreInput(
            total_uses=5, total_failures=0, last_success_at=recent,
        )
        result = compute_experience_score(inp, now=now)
        assert result.score > 1.0 - 0.001  # should get recency bonus

    def test_no_recency_when_stale(self):
        now = datetime.datetime(2026, 9, 11, tzinfo=datetime.timezone.utc)
        old = now - datetime.timedelta(days=90)
        inp_recent = ExperienceScoreInput(
            total_uses=5, total_failures=0,
            last_success_at=now - datetime.timedelta(days=5),
        )
        inp_stale = ExperienceScoreInput(
            total_uses=5, total_failures=0,
            last_success_at=old,
        )
        result_recent = compute_experience_score(inp_recent, now=now)
        result_stale = compute_experience_score(inp_stale, now=now)
        assert result_recent.score >= result_stale.score

    def test_score_clamped_to_0_1(self):
        inp = ExperienceScoreInput(total_uses=100, total_failures=200)
        result = compute_experience_score(inp)
        assert result.score >= 0.0
        assert result.score <= 1.0


class TestShouldRecommendReplacement:

    def test_below_threshold(self):
        score = ExperienceScoreResult(score=0.2, success_rate=0.5,
                                       total_uses=10, total_failures=5)
        assert should_recommend_replacement(score) is True

    def test_above_threshold(self):
        score = ExperienceScoreResult(score=0.8, success_rate=0.9,
                                       total_uses=10, total_failures=1)
        assert should_recommend_replacement(score) is False

    def test_zero_uses(self):
        score = ExperienceScoreResult(score=0.0, success_rate=1.0,
                                       total_uses=0, total_failures=0)
        assert should_recommend_replacement(score) is False


class TestSummarizeUsage:

    def test_not_used(self):
        result = summarize_usage(
            uses=[], failures=[], score=None,
            capability_id="cap1", project_id="proj1",
        )
        assert result.recommendation == "not_used"
        assert result.active_uses == 0

    def test_high_score_continue(self):
        score = ExperienceScoreResult(score=0.9, success_rate=1.0,
                                       total_uses=5, total_failures=0)
        result = summarize_usage(
            uses=[{"status": "active"}, {"status": "active"}],
            failures=[],
            score=score,
            capability_id="cap1", project_id="proj1",
        )
        assert result.recommendation == "continue"
        assert result.active_uses == 2

    def test_medium_score_monitor(self):
        score = ExperienceScoreResult(score=0.5, success_rate=0.7,
                                       total_uses=10, total_failures=3)
        result = summarize_usage(
            uses=[{"status": "active"}],
            failures=[{"resolved_at": None}],
            score=score,
            capability_id="cap1", project_id="proj1",
        )
        assert result.recommendation == "monitor"
        assert result.unresolved_failures == 1

    def test_low_score_replacement(self):
        score = ExperienceScoreResult(score=0.1, success_rate=0.2,
                                       total_uses=10, total_failures=8)
        result = summarize_usage(
            uses=[{"status": "failed"}],
            failures=[{"resolved_at": None}, {"resolved_at": "2026-01-01"}],
            score=score,
            capability_id="cap1", project_id="proj1",
        )
        assert result.recommendation == "consider_replacement"

    def test_counts_unresolved_correctly(self):
        score = ExperienceScoreResult(score=0.5, success_rate=0.5,
                                       total_uses=4, total_failures=2)
        result = summarize_usage(
            uses=[{"status": "active"}],
            failures=[
                {"resolved_at": None},
                {"resolved_at": "2026-01-01"},
                {"resolved_at": None},
            ],
            score=score,
            capability_id="cap1", project_id="proj1",
        )
        assert result.unresolved_failures == 2
        assert result.total_failures == 3


class TestEnums:

    def test_use_status_values(self):
        assert UseStatus.ACTIVE.value == "active"
        assert UseStatus.RETIRED.value == "retired"
        assert UseStatus.REPLACED.value == "replaced"
        assert UseStatus.FAILED.value == "failed"

    def test_failure_kind_values(self):
        assert FailureKind.INSTALL_FAILURE.value == "install_failure"
        assert FailureKind.RUNTIME_ERROR.value == "runtime_error"
        assert FailureKind.SECURITY_VULNERABILITY.value == "security_vulnerability"

    def test_failure_severity_values(self):
        assert FailureSeverity.WARNING.value == "warning"
        assert FailureSeverity.ERROR.value == "error"
        assert FailureSeverity.CRITICAL.value == "critical"


# ---------------------------------------------------------------------------
# Seed helpers (DB tests)
# ---------------------------------------------------------------------------

def _mk_project(conn, name: str) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO project (name) VALUES (%s) RETURNING id",
            (name,),
        )
        return cur.fetchone()[0]


def _mk_capability(conn, key: str, name: str) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability "
            "(normalized_key, display_name, ecosystem, kind) "
            "VALUES (%s, %s, 'pypi', 'library') RETURNING id",
            (key, name),
        )
        return cur.fetchone()[0]


def _mk_version(conn, capability_id: uuid.UUID) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability_version "
            "(capability_id, version_key, version_kind, display_version, lifecycle_state) "
            "VALUES (%s, %s, 'content-hash', '1.0.0', 'candidate') RETURNING id",
            (capability_id, f"content:{uuid.uuid4().hex[:8]}"),
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# DB-backed experience query tests
# ---------------------------------------------------------------------------

class TestRecordComponentUse:

    def test_records_use(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        cap = _mk_capability(conn, "pypi:exp-a", "exp-a")
        ver = _mk_version(conn, cap)
        conn.commit()

        result = queries.record_component_use(
            conn,
            project_id=str(proj),
            capability_id=str(cap),
            capability_version_id=str(ver),
        )
        assert result is not None
        assert result["status"] == "active"
        assert result["project_id"] == str(proj)
        assert result["capability_id"] == str(cap)

    def test_bad_ids_return_none(self, conn):
        result = queries.record_component_use(
            conn, project_id="bad", capability_id="bad",
        )
        assert result is None


class TestRetireComponentUse:

    def test_retires_active_use(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        cap = _mk_capability(conn, "pypi:exp-b", "exp-b")
        conn.commit()

        use = queries.record_component_use(
            conn, project_id=str(proj), capability_id=str(cap),
        )
        result = queries.retire_component_use(
            conn, use_id=use["use_id"], reason="upgrade",
        )
        assert result is not None
        assert result["status"] == "retired"
        assert result["reason"] == "upgrade"

    def test_cannot_retire_already_retired(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        cap = _mk_capability(conn, "pypi:exp-c", "exp-c")
        conn.commit()

        use = queries.record_component_use(
            conn, project_id=str(proj), capability_id=str(cap),
        )
        queries.retire_component_use(conn, use_id=use["use_id"])
        result = queries.retire_component_use(conn, use_id=use["use_id"])
        assert result is None

    def test_bad_id_returns_none(self, conn):
        result = queries.retire_component_use(conn, use_id="not-a-uuid")
        assert result is None


class TestLogFailureEvent:

    def test_logs_failure(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        cap = _mk_capability(conn, "pypi:exp-d", "exp-d")
        conn.commit()

        use = queries.record_component_use(
            conn, project_id=str(proj), capability_id=str(cap),
        )
        result = queries.log_failure_event(
            conn,
            use_id=use["use_id"],
            failure_kind="import_failure",
            severity="error",
            summary="ModuleNotFoundError",
            detail="No module named 'exp_d'",
        )
        assert result is not None
        assert result["failure_kind"] == "import_failure"
        assert result["severity"] == "error"

    def test_bad_id_returns_none(self, conn):
        result = queries.log_failure_event(
            conn, use_id="bad", failure_kind="other",
        )
        assert result is None


class TestResolveFailureEvent:

    def test_resolves_failure(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        cap = _mk_capability(conn, "pypi:exp-e", "exp-e")
        conn.commit()

        use = queries.record_component_use(
            conn, project_id=str(proj), capability_id=str(cap),
        )
        failure = queries.log_failure_event(
            conn, use_id=use["use_id"],
            failure_kind="runtime_error", summary="crash",
        )
        result = queries.resolve_failure_event(
            conn, event_id=failure["event_id"],
            resolution="upgraded to v2",
        )
        assert result is not None
        assert result["resolved"] is True

    def test_cannot_resolve_twice(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        cap = _mk_capability(conn, "pypi:exp-f", "exp-f")
        conn.commit()

        use = queries.record_component_use(
            conn, project_id=str(proj), capability_id=str(cap),
        )
        failure = queries.log_failure_event(
            conn, use_id=use["use_id"], failure_kind="other",
        )
        queries.resolve_failure_event(conn, event_id=failure["event_id"])
        result = queries.resolve_failure_event(
            conn, event_id=failure["event_id"],
        )
        assert result is None


class TestExperienceSummary:

    def test_full_summary(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        cap = _mk_capability(conn, "pypi:exp-g", "exp-g")
        _mk_version(conn, cap)
        conn.commit()

        use = queries.record_component_use(
            conn, project_id=str(proj), capability_id=str(cap),
        )
        queries.log_failure_event(
            conn, use_id=use["use_id"],
            failure_kind="runtime_error", summary="intermittent crash",
        )

        result = queries.experience_summary(
            conn, project_id=str(proj), capability_id=str(cap),
        )
        assert result is not None
        assert result["active_uses"] == 1
        assert result["total_failures"] == 1
        assert result["unresolved_failures"] == 1
        assert result["experience_score"] >= 0.0
        assert result["recommendation"] in ("continue", "monitor",
                                              "consider_replacement",
                                              "not_used")

    def test_no_uses(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        cap = _mk_capability(conn, "pypi:exp-h", "exp-h")
        _mk_version(conn, cap)
        conn.commit()

        result = queries.experience_summary(
            conn, project_id=str(proj), capability_id=str(cap),
        )
        assert result is not None
        assert result["active_uses"] == 0
        assert result["recommendation"] == "not_used"

    def test_updates_score_on_read(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        cap = _mk_capability(conn, "pypi:exp-i", "exp-i")
        _mk_version(conn, cap)
        conn.commit()

        queries.record_component_use(
            conn, project_id=str(proj), capability_id=str(cap),
        )
        queries.record_component_use(
            conn, project_id=str(proj), capability_id=str(cap),
        )

        result = queries.experience_summary(
            conn, project_id=str(proj), capability_id=str(cap),
        )
        assert result["experience_score"] >= 0.9

    def test_bad_ids_return_none(self, conn):
        result = queries.experience_summary(
            conn, project_id="bad", capability_id="bad",
        )
        assert result is None

    def test_nonexistent_capability_returns_none(self, conn):
        proj = _mk_project(conn, f"proj-{uuid.uuid4().hex[:6]}")
        conn.commit()
        fake_cap = str(uuid.uuid4())
        result = queries.experience_summary(
            conn, project_id=str(proj), capability_id=fake_cap,
        )
        assert result is None
