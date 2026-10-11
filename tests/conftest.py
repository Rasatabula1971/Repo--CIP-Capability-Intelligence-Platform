"""
Test fixtures.

Every test gets a clean database. We truncate rather than drop/recreate —
the migrations are already applied once at session start, and truncating
is much faster.
"""
from __future__ import annotations

import pytest

from db.connection import connect
from db import migrate

TABLES_IN_TRUNCATE_ORDER = [
    "evaluation",
    "audit_event",
    "approval",
    "approval_request",
    "decision_record",
    "requirement_search",
    "acceptance_criterion",
    "requirement_need_link",
    "requirement_verification",
    "build_task_requirement",
    "build_task",
    "architecture_lock",
    "requirement_source",
    "capability_symbol",
    "adapter_spec",
    "inspection_run",
    "experience_score",
    "failure_event",
    "component_project_use",
    "verification_assertion",
    "verification_run",
    "workflow_template_step",
    "component_score",
    "capability_link",
    "component_classification",
    "ingest_run",
    "build_authorization",
    "recommendation_candidate",
    "recommendation",
    "recommendation_rules_profile",
    "fit_evidence_link",
    "fit_gap",
    "fit_evaluation",
    "requirement_constraint",
    "project_constraint",
    "project_requirement",
    "project",
    "risk_acceptance",
    "gate_result",
    "score_evidence_link",
    "score_dimension_result",
    "scorecard",
    "scoring_profile",
    "capability_dependency",
    "capability_interface",
    "capability_source_binding",
    "capability_version",
    "capability",
    "judgment_error",
    "judgment_response",
    "judgment_request",
    "model_profile",
    "model_provider",
    "evidence_item",
    "analysis_stage_result",
    "analysis_run",
    "dead_letter_item",
    "outbox_event",
    "workflow_command",
    "state_transition",
    "workflow_step_attempt",
    "workflow_run",
    "file_artifact",
    "source_revision",
    "source_asset",
    "source_provider",
]


@pytest.fixture(scope="session", autouse=True)
def _apply_migrations():
    migrate.up(for_tests=True)


@pytest.fixture(autouse=True)
def _wide_terminal(monkeypatch):
    """
    The CLI wraps every line it prints to the terminal's width. Under
    pytest there is no terminal, so that would be 80 columns and every
    assertion on a long line would be about wrapping. Tests that ARE
    about wrapping set their own width.
    """
    from cip_composer import cli
    monkeypatch.setattr(cli, "_terminal_width", lambda: 100_000)


@pytest.fixture
def conn():
    """A clean, non-autocommit connection to the test DB."""
    _truncate_all()
    c = connect(for_tests=True)
    try:
        yield c
    finally:
        c.close()


def _truncate_all():
    c = connect(for_tests=True)
    try:
        with c.cursor() as cur:
            # RESTART IDENTITY resets outbox_event's serial.
            cur.execute(
                "TRUNCATE " + ", ".join(TABLES_IN_TRUNCATE_ORDER) +
                " RESTART IDENTITY CASCADE"
            )
        c.commit()
    finally:
        c.close()
